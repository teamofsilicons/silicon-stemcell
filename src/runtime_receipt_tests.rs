use super::*;
use std::io::{BufRead, BufReader, Write};

struct Fixture {
    runtime: Arc<Runtime>,
    connected: Arc<Connected>,
    worker: Arc<Worker>,
}

impl Fixture {
    fn new() -> Self {
        let mut cfg: Config = serde_json::from_value(json!({
            "silicon": {"id": "receipt-tests"},
            "isi": {"udd": {}}, "access": {}, "flow": []
        }))
        .unwrap();
        cfg.home = std::env::temp_dir().join(format!("silicon-receipts-{}", Uuid::new_v4()));
        cfg.generation = Uuid::new_v4();
        state::private_dir(&cfg.home).unwrap();
        let connected = Arc::new(Connected {
            ting: crate::ting::Inbox::new(&cfg).unwrap(),
            outbox: crate::outbox::Outbox::new(&cfg).unwrap(),
            app_settings: RwLock::new(cfg.clone()),
            generation: cfg.generation,
            cfg,
            pending_outgoing: Mutex::new(HashMap::new()),
            workers: Mutex::new(BTreeMap::new()),
            enabled: AtomicBool::new(true),
            beats: Mutex::new(BTreeMap::new()),
            beats_changed: AtomicBool::new(false),
            beats_file: Mutex::new(None),
            reported: Mutex::new(HashMap::new()),
        });
        let runtime = Runtime::new("http://unused.invalid".into());
        let record = Session::new("udd", None, "receipt test", false, false);
        let worker = Arc::new(Worker {
            connected: Arc::downgrade(&connected),
            runtime: Arc::downgrade(&runtime),
            session_id: record.session_id,
            state: Mutex::new(WorkerState::new(record)),
            client: Mutex::new(None),
            daemon: Mutex::new(None),
            capability: String::new(),
            stopped: AtomicBool::new(false),
            retired: AtomicBool::new(false),
        });
        connected
            .workers
            .lock()
            .unwrap()
            .insert(worker.session_id, worker.clone());
        Self {
            runtime,
            connected,
            worker,
        }
    }

    fn send(&self, message: &str, after_seq: i64, client: Client) -> Sent {
        let mut state = self.worker.state.lock().unwrap();
        let dispatch = pending(message, after_seq);
        let sent = Sent {
            session: state.record.clone(),
            id: dispatch.id,
            receipt: dispatch.receipt.clone(),
            worker: Arc::downgrade(&self.worker),
            client,
        };
        state.pending.push(dispatch);
        state.record.status = "running".into();
        sent
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        fs::remove_dir_all(&self.connected.cfg.home).unwrap();
    }
}

fn pending(message: &str, after_seq: i64) -> Dispatch {
    Dispatch {
        id: Uuid::new_v4(),
        message: message.into(),
        started_seq: None,
        after_seq,
        receipt: Arc::new(SendReceipt {
            result: Mutex::new(None),
            changed: Condvar::new(),
            deadline: Instant::now(),
        }),
        origin: None,
        heartbeat: false,
    }
}

fn event(kind: &str, seq: i64, text: &str) -> Event {
    let mut event = Event::new(kind).saying(text);
    event.seq = seq;
    event.turn = 0;
    event
}

fn history(events: Vec<Event>) -> (Client, thread::JoinHandle<Request>) {
    let (ours, mut theirs) = UnixStream::pair().unwrap();
    theirs
        .set_read_timeout(Some(Duration::from_secs(5)))
        .unwrap();
    let client = Client::from_stream(ours).unwrap();
    let server = thread::spawn(move || {
        let mut line = String::new();
        BufReader::new(theirs.try_clone().unwrap())
            .read_line(&mut line)
            .unwrap();
        let request: Request = serde_json::from_str(&line).unwrap();
        assert_eq!(request.op, "events", "receipt recovery must not resend");
        writeln!(
            theirs,
            "{}",
            json!({
                "id": request.id, "ok": true, "result": {"events": events}
            })
        )
        .unwrap();
        request
    });
    (client, server)
}

#[test]
fn expired_receipt_recovers_durable_start_without_the_send_lock() {
    let fixture = Fixture::new();
    let (client, server) = history(vec![event(Event::START, 6, "Heyy\n")]);
    let sent = fixture.send("Heyy\n", 5, client);
    let _sending = fixture.worker.client.lock().unwrap();
    sent.wait_started().unwrap();
    let request = server.join().unwrap();
    assert_eq!(request.from, Some(6));
    assert_eq!(request.session, Some(fixture.worker.session_id.to_string()));
    assert_eq!(
        fixture.worker.state.lock().unwrap().pending[0].started_seq,
        Some(6)
    );
}

#[test]
fn missing_durable_receipt_stays_uncertain_without_resending() {
    let fixture = Fixture::new();
    let (client, server) = history(vec![event(Event::TEXT, 6, "response")]);
    let sent = fixture.send("Heyy", 5, client);
    assert!(sent.wait_started().unwrap_err().is::<ReceiptPending>());
    server.join().unwrap();
    assert!(fixture.worker.state.lock().unwrap().pending[0]
        .started_seq
        .is_none());
}

#[test]
fn uncertain_delivery_after_restart_never_dispatches_again() {
    let fixture = Fixture::new();
    let error = fixture
        .runtime
        .deliver_outgoing(
            &fixture.connected,
            "old-run/0",
            &flow::Delivery {
                isi: "not-configured".into(),
                session_id: None,
                message: "already accepted".into(),
            },
            true,
        )
        .unwrap_err();
    assert!(error.is::<crate::outbox::AwaitingReceipt>());
    assert!(fixture
        .connected
        .pending_outgoing
        .lock()
        .unwrap()
        .is_empty());
}

#[test]
fn missing_earlier_duplicate_is_recovered_before_later_live_receipt() {
    let fixture = Fixture::new();
    let later = event(Event::INJECTED, 7, "same");
    let (client, server) = history(vec![event(Event::START, 6, "same"), later.clone()]);
    let first = fixture.send("same", 5, client.clone());
    let second = fixture.send("same", 6, client.clone());
    fixture.worker.on_event(later.clone(), false, 0).unwrap();
    assert!(first.receipt.wait().unwrap_err().is::<ReceiptPending>());
    assert!(second.receipt.wait().unwrap_err().is::<ReceiptPending>());
    fixture.worker.recover_receipts(&client, 7).unwrap();
    fixture.worker.on_event(later, false, 0).unwrap();
    server.join().unwrap();
    first.receipt.wait().unwrap();
    second.receipt.wait().unwrap();
    let state = fixture.worker.state.lock().unwrap();
    assert_eq!(state.pending[0].started_seq, Some(6));
    assert_eq!(state.pending[1].started_seq, Some(7));
}

#[test]
fn only_new_nonlate_receipts_confirm_each_duplicate_once() {
    let mut state = WorkerState::new(Session::new("udd", None, "test", false, false));
    state.pending = vec![pending("same", 5), pending("same", 5)];
    for ignored in [
        event(Event::START, 5, "same"),
        event(Event::START, 6, "other"),
        event(Event::TEXT, 6, "same"),
        event(Event::END, 6, "same"),
        event(Event::CONFIG, 6, "same"),
        event(Event::START, 6, "same").with("late", true),
    ] {
        state.confirm_receipt(&ignored);
    }
    assert!(state
        .pending
        .iter()
        .all(|dispatch| dispatch.started_seq.is_none()));
    let first = event(Event::START, 6, "same");
    state.confirm_receipt(&first);
    state.confirm_receipt(&first);
    assert_eq!(state.pending[0].started_seq, Some(6));
    assert!(state.pending[1].started_seq.is_none());
    state.confirm_receipt(&event(Event::INJECTED, 7, "same"));
    assert_eq!(state.pending[1].started_seq, Some(7));
}

#[test]
fn receipt_recovered_after_idle_end_retires_finished_work() {
    let fixture = Fixture::new();
    let (client, server) = history(vec![event(Event::START, 6, "Heyy")]);
    let sent = fixture.send("Heyy", 5, client);
    fixture.worker.state.lock().unwrap().pending[0].heartbeat = true;
    fixture
        .worker
        .on_event(event(Event::TEXT, 7, "done"), false, 0)
        .unwrap();
    fixture
        .worker
        .on_event(event(Event::END, 8, ""), true, 0)
        .unwrap();
    assert!(heartbeat_unfinished(&fixture.connected, "udd", None));
    sent.wait_started().unwrap();
    server.join().unwrap();
    assert!(!heartbeat_unfinished(&fixture.connected, "udd", None));
    fixture.worker.finish_deliveries().unwrap();
    let state = fixture.worker.state.lock().unwrap();
    assert!(state.pending.is_empty());
    assert!(state.finished.is_empty());
    assert_eq!(state.record.status, "idle");
}

#[test]
fn older_end_cannot_finish_a_newer_dispatch_learned_from_history() {
    let fixture = Fixture::new();
    let (client, server) = history(vec![
        event(Event::START, 6, "first"),
        event(Event::START, 8, "second"),
    ]);
    fixture.send("first", 5, client.clone());
    let second = fixture.send("second", 7, client.clone());
    fixture.worker.recover_receipts(&client, i64::MAX).unwrap();
    server.join().unwrap();
    fixture
        .worker
        .on_event(event(Event::END, 7, ""), true, 0)
        .unwrap();
    {
        let state = fixture.worker.state.lock().unwrap();
        assert_eq!(state.pending.len(), 1);
        assert_eq!(state.pending[0].id, second.id);
        assert!(state.turn_open);
        assert_eq!(state.record.status, "running");
    }
    fixture
        .worker
        .on_event(event(Event::END, 9, ""), true, 0)
        .unwrap();
    assert!(fixture.worker.state.lock().unwrap().pending.is_empty());
}

#[test]
fn delayed_receipts_keep_the_reply_from_their_own_completed_turn() {
    let mut state = WorkerState::new(Session::new("udd", None, "test", false, false));
    state.pending = vec![pending("first", 5), pending("second", 8)];
    state.finished.insert(8, "first reply".into());
    state.finished.insert(11, "second reply".into());
    state.confirm_receipt(&event(Event::START, 9, "second"));
    let finished = state.take_finished();
    assert_eq!(finished.len(), 1);
    assert_eq!(finished[0].1, "second reply");
    assert_eq!(state.finished.len(), 2);
    state.confirm_receipt(&event(Event::START, 6, "first"));
    let finished = state.take_finished();
    assert_eq!(finished.len(), 1);
    assert_eq!(finished[0].1, "first reply");
    assert!(state.finished.is_empty());
    assert!(state.pending.is_empty());
}
