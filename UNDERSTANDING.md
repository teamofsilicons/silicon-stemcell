[silicon interpreter 3.5]
    |
---------
|   |   |
s1  s2  s3
|a  |a  |c  
|b  |x  |a
|c


silicon interpreter starts a web server at localhost:1823 (-1 until you find an available port) and maps it to silicon.localhost using caddy.
any new silicon wanting to join silicon interpreter needs to pass the silicon.yaml to use. this yaml file's location is its local identity. a silicon's global identity is silicon id, which it can authenticate using silicon token.

silicon interpreter offers a way for silicons to connect & disconnect.
silicon interpreter is a command line first interface. i will be showing all thigns on the command line, but same should be possible to do on the web as well.

silicon_id is like this `si:{handle}`. `silicon.org_id` carries the owning org separately; never infer it from the id. Carbon ids use `c:{handle}`, and app ids are bare handles like `dm`. Bundle ids retain `org>bundle`.

connect:
`silicon connect {path to yaml}`
compiles the silicon.yaml and throws errors if any, or connect if all is good.
create a local listening url ({handle}.{org_id}.localhost when both are DNS labels) for this silicon and maps that url to silicon interpreter's web server link. Use the connection's returned host for handles or organizations that need DNS encoding.

Once the local route is ready, register its URL with Ting using the Honeycomb-installed `ting` CLI. Ting is implicit, like IAM, even when absent from apps.

show the output of all scripts inside setup, tell when each app is installed and logged in, and when webhooks are setup.

disconnect:
`silicon disconnect {path to yaml}` stops & disconnect a silicon
`silicon disconnect` shows a list of silicons connected (silicon id + yaml path) and asks to run the next command
`silicon disconnect {sid}` stops & disconnect a silicon

list:
`silicon ls` lists all connected silicons
`silicon ls 'si:abc*'` list all matching

logs:
`silicon logs show {sid}` shows the last 100 lines and starts following the logs. displays log location & silicon id at the bottom persistently.

install honeycomb apps
`silicon install appid` installs the app from honeycomb for this distribution

uninstall
`silicon uninstall appid`


Events:
The local webhook accepts any valid JSON value, not just Ting batches. Preserve that value as request, save it durably, return empty HTTP 204, then execute the current flow. Keep the 1 MiB limit, Content-Type/origin protections, and bounded durable inbox (10,000 requests or 256 MiB); temporary inability to accept returns 503 with Retry-After.

Ting sends {tings: [{id: str, type: str, data: obj, metadata: obj}, ...]}. Deduplicate IDs only for an exact tings envelope with 1–100 fully valid records (id is 1–256 bytes, type is nonempty). All other JSON—including malformed tings and extra envelope fields—is generic input, preserved whole and never deduplicated by Ting ID.

Flow send queues messages by default and aggregates them per (isi, session_id), in order, at the end of the flow. aggregate: false flushes earlier queued messages to that same destination and attempts the current message immediately. Flushed messages can enter an active provider turn; no inference completion is required. Function return/failure does not roll back queued messages.

Persist outgoing messages before dispatch in .silicon/outbox/<silicon-id>/<org>/<request-id>.json. silicon.max_retries defaults to 10 retries after the first attempt. Record failures and recovery, retain undelivered groups after exhaustion, and only retry the stash when the next message is sent to that same isi+session. No timer-driven fallback or redirection to another destination after exhaustion. Delivery receipts are persisted so a retried incoming request can reuse its outgoing state. A crash before a receipt is saved may still duplicate delivery; app/shell side effects are not transactional.

Logs:
Log everything. New msg, what's happening in flow, what each of the isi is doing, everything. Omni has a event that you can subscribe to and use for loggin the active work. when displaying, color code different isi msg, runtime logs, errors. not the complete msg, but just the first section. include metadata with the logs like [type] [origin] [timestamp] [message]



Updates:
this is an open source project, make the versions live on github, and keep checking for updates every 1hr, and then update as and when a new update is pushed.

what we update is just the interpretter, cli, tos deamon, etc. and that's it. never make any changes to a silicon.yaml


Default:
the silicon, memories, & workspace folders are templates. this is not sent to anyone. this is here just for testing.


Shipment:
everything needed to run silicon should be bundled here. Then a single curl + sh command should intall all things required on the local system to get things up and running. Write a docs for how to use silicon, the config docs, expectations from IAM apps. etc etc.

everything should be on docs.teamofsilicons.com on vercel (i have cli installed and logged in)


Installation:
Anyone should be able to run one bash command to install all of silicon & dependencies of it.
This includes the interpretter, caddy, iam, omni, dm, briefcase, waveform, commit, remind & hook.



# silicon.yaml parser
required top level keys:
- silicon: contains silicon specific things. this applies to all isi. this is the brain.
- isi: these are internal silicons. almost like brain regions.
- access: this is like corpus callosum. that tells who can talk with who. this is a part of prompt we feed in from silicon's side. 
- flow: this is the runtime flow. msg comes in -> do x -> do y

optional top level key:
- functions: named reusable YAML step bodies, or external definition file paths. It may appear before or after flow; resolve definitions before executing steps.

entire silicon.yaml file is checked for syntax errors.
silicon, isi and access are compile time accessed.
flow and functions are runtime-loaded together; edits to either the YAML or imported definition files take effect on the next request.

silicon:
    id                      full silicon id, si:handle
    org_id                  explicit IAM owning organization
    token                   silicon token, used for authentication
    timezone                timezone silicon operates in
    max_retries             optional nonnegative integer; default 10 retries after first outgoing flow delivery attempt
    SILICON_HOME            env variable passed for all ISI, commands are executed reletive to this
    SILICON_ORG             env variables passed for all ISI; defaults to org_id
    space_station           optional; for telemetry
        table_name          space station table name
        table_key           space station table key
    inference_providers     omni supported inference providers
    setup                   optional; list of shell command that is run once during connecting silicon
    apps                    optional; list of bare iam app ids, automatically logged in using silicon token
    app_configs             optional; sets key value pairs for apps

inference_providers can take in either a list of inference providers that omni supports.
it can take - all-available-providers
and it can use - except provider
and it is all nestable.

inference_providers:
    - all-available-providers
    - except codex-app-server
    - claude-code-cli

now, ideally all-available-providers should already have claude code cli, but later if groups are introduced like open-weight-models and then i wanna keep only one.
move from top to bottom, and add / remove from the group.
if only all-available-providers is written, use the native option instead of computing.

apps mentioned inside `apps`, plus implicit IAM and Ting, should be automatically installed from honeycomb. Apps and Ting are logged in using the IAM CLI.

app's follow iam auth methods.
`app iam --json` -> extract app_id -> generate a short lived auth token for this app_id -> `app login "..."` -> check -> `app login status --json` and check authenticated: true

app config setup:
every app with configs supports running `app config set "{key: value, ...}"`

once a silicon is ready to receive messages, register it on ting.

apps themselves manage access & refresh tokens.
any isi can login to an app assisted by `si setup auth app_id` cli
before heartbeats & new sessions, all auths are checked and authenticated if needed.

when disconnecting a silicon, run `ting unhook {webhook_id}`. Retain the ID and reuse it when reconnecting.

isi:
each isi is run on its own terminal with SILICON_HOME, SILICON_ORG & ISI
if isi has primary send mode global, its just the isi name, if it is session, then its name:id

    isi_name:                       name that other allowed isi can call this isi by. min 1 req.
        model                       omni model it will use based on the provider
        primary_send_mode           global or session. session_id is req. to send when session
        session_type                persistent or ephemeral. when turn ends, does it auto archive or not
        dna:                        prompt sent
            assemble                list of file-loc relative SILICON_HOME, bash scripts echoing text
            next_refresh            refresh this dna after X min. can be bash. eval on each refresh
        heartbeat:                  optional; a regular heartbeat for isi
            next                    next heartbeat in X min. can be bash.
            message                 message to send during heartbeat.
        new_session_suggestion:     optional; suggest to start a new session
            cooldown_minutes        min. duration between last send suggestion & now
            min_new_messages        min. new messages in that session. heartbeat & sessions dont count.
            suggestion_message      what msg to send as suggestion.


dna assemble is a list & accepts file location reletive to SILICON_HOME, a bash script with pwd SILICON_HOME and ISI. & fallbacks using !>>

dna assemble:
```txt
{assemble item1 verbaitim (eg: ! ./contacts.sh !>> "You have no contacts")}
{expression computed / file contents / ...}


{assemble item2 verbaitim (eg: ! ./contacts.sh !>> "You have no contacts")}
{expression computed / file contents / ...}
...
```

there is no default heartbeat or new_session_suggestion for isi with an undefined one.


access:
    isi1: [isi2, isi5]      list of isi the given isi can message using the `si send` command.

this must be defined for all isi

flow:
The flow is a list of steps. Function bodies, loop bodies, then, and catch all use the same step lists. Strings support CEL; existing expression-generated steps and ! commands (including Python files) still work.

if:
    condition           boolean expression
    then                list of next steps
    else                optional list of steps
    catch               optional error handler; self.error is the complete error string
else:                   standalone list; runs if no immediately preceding consecutive if matched

var:
    name                variable name, accessible as var.name
    value               any JSON-compatible value, recursively evaluated

for:
    list                expression evaluating to a list
    var                 name of current item, accessible as var.NAME
    then                list of steps per item
                        iteration variables are local; self.for.index is zero-based

switch:
    value               value to compare
    cases               list of {case: VALUE, then: [STEPS]}; first matching case runs
    default             optional list of steps when none match

collect:
    name                list variable in the enclosing loop's parent scope
    value               append this JSON-compatible value; create the list if absent
                        existing target must be a list; result survives the loop

continue:
    reason              required nonempty message, evaluated and logged; next loop item
break:
    reason              required nonempty message, evaluated and logged; leave nearest loop
exit:
    reason              required nonempty message, evaluated and logged; finish entire flow
                        flush already queued messages; does not disconnect the Silicon

send:
    isi                 destination ISI
    session_id          required for primary_send_mode session; creates a missing session
    message             message expression
    aggregate           optional boolean; default true; false requests immediate delivery
    catch               preflight failures only: undefined ISI, missing required session ID,
                        invalid message expression, etc. Actual delivery belongs to runtime
                        recovery and never reaches this catch, even for aggregate: false.

log:
    message             append to silicon.log

call:
    function            name registered in functions
    args                optional mapping of arguments
    then                optional list of steps; returned value is self.result
    catch               optional list of steps; unhandled function/evaluation error is self.error

return: VALUE           finish current function with any JSON-compatible value; no return means null

Functions are optional and separate from flow. YAML key order does not affect availability:

```yaml
flow:
  - call:
      function: greeting
      args: {name: '{request.name}'}
      then:
        - var: {name: saved, value: '{self.result}'}
      catch:
        - log: {message: '{self.error}'}

functions:
  greeting:
    params: [name]
    do:
      - return: {message: 'Hello, {args.name}'}
```

Each function call has local var and args; all functions share the flow's send queue. self.result is scoped to then, self.error to catch. Nested calls/catches restore outer self values afterward. Explicit var steps retain a result for later. Unhandled function errors propagate to call.catch, while ordinary root flow errors log and skip the failed step. No transaction rolls back earlier steps or queued sends.

External definitions use functions: ./functions.yaml, where the file contains the bare function map. A list can combine sources: functions: [./shared.yaml, {local_name: {params: [], do: []}}]. Imports resolve relative to their containing file and can include further paths/lists; duplicate names and import cycles are errors. Load and validate all definitions before running the flow, and reload changed files on the next request.

Migration: replace {error} with {self.error}; send is now aggregated by default, and send.catch no longer reports provider delivery failures. The local webhook no longer rejects JSON solely for lacking a valid Ting envelope.


# logging
keep a log of everything that is happening inside SILICON_HOME/.silicon/silicon.log

### Parsing & compilation

#### bash:
any string can be replaced by a bash command by adding !
> description: some description here
or
> description: ! cat description.md
this runs `cat description.md` inside bash, with pwd SILICON_HOME and ISI if run inside one of the isi blocks.
dna assemble also accepts a file location reletive to SILICON_HOME.
- ../memories/learnings/workers/advertising.md
inputs the file's content

#### evals
evaluation order: CEL -> Bash -> String
any string with non excaped {...} should be evaluated using CEL. Pass the following to it:
- request (this is json that was received on the Silicon's local URL, such as http://assistant.my-org.localhost/)
- silicon, isi and access as json
- tz_time function which takes in a UTC time, and a timezone in IANA, and outputs time in that timezone. {HH:MM:SS DD:MM:YY IANA}
- to_yaml takes in json, and prints it with tabs & new lines (yaml).
- to_json takes in string, and evaluates it so it can become a json and be evaluatable.
- .sortBy, .join, .distinct, .slice, .reverse, .flatten work with lists.
- .groupBy(item, expression) returns [{key, items}] for any JSON key; groups preserve first key occurrence and items preserve input order.
- args contains current function arguments; var contains current-scope variables; self.result, self.error and self.for.index are scoped to their respective branches/loop.



#### fallbacks
anything that is evaluated, can fail. so we have a fallback for those expressions.
since evaluations needs to evaluate to strings, strings are also valid fallbacks.

> ! ./contacts.sh !>> ! cat CONTACTS.md !>> "No contacts found"

error is not passed, its just a fallback. if not A, then B, if not B, then C.
errors are logged and we move to the next fallback available.

if all fallbacks fail, skip and move ahead.

#### errors
Flow evaluation failures support catch with scoped self.error. Log the full error. Outside functions, an unhandled step failure logs and continues; inside functions, propagate it to the caller. Actual send delivery failures are persisted and handled by the runtime, never send.catch.


#### dependencies
Silicon Omni – Inference Provider. Use their rust package.
https://omni.teamofsilicons.com/

Silicon IAM – Identify & Access Management which allows authentication.
Install and Use their CLI. Use with SILICON_HOME



# External Expectation (from iam apps)
CLIs respect SILICON_HOME and store each their local states inside that folder itself. Its home, so they should use that as base, and make their own hidden folders to keep their information.

Specific apps that could benefit from using ISI should do that. eg: dm.

All iam apps' authentication is managed by silicon interpretter.
`app iam --json` gives {app_id: "..."} along with other info
`app login "..."` takes in a short lived auth token generated by silicon interpretter.
`app login status --json` tells if its {authenticated: true}

Events:
Apps publish notifications through Ting, which owns its registration and delivers `{"tings": [...]}` batches. Each ting carries id, type, data and metadata. Other callers may POST any JSON to the same local endpoint. The interpreter durably accepts input before empty HTTP 204, then executes the current flow and function definitions from disk.


# si cli
si {service} {verb} [{target}] [{content}] [--flags]
this is injected for each isi to do internal things.

> auth:
`si auth --help`
`si auth setup {app_command}` used as something like `si auth setup dm` to log into dm if dm is every logged out. this way isi dont need to see the silicon token to authenticate.
`si auth remove {app_command}` to unauthenticate.

> isi:
`si isi --help`
`si isi ls {isi}` shows the active sessions of a given isi
`si isi ls {isi} --archived` shows the last 3 days of archived sessions of a given isi
--archived DD:MM:YYY for archived on that day, --archived DD:MM:YYY-DD:MM:YYY to get between the 2 dates, --archived "*code" to search a given keyword in title and description. these are stackable with --archived "foot*" DD:MM:YYY-DD:MM:YYY.
this can be use for other isi, and self to ask something from a previsous session of this isi.

`si isi show {isi}` to see the current progress of an isi without asking
`si isi end {isi}` to end its current session (doesn't start a new one)

global & persistent:
`si isi send {isi} "..."` send a new message. starts a new session if not already.

global & ephemeral:
`si isi send {isi} "..."` send a new message. starts a new session. automatically sends the turn end output to the isi. it is not archived. it is use & throw.

session & persistent:
`si isi send {isi} "..." --id "..."` to send a msg to a non-archived isi based on id. throws an error if id is not found.
`si isi send {isi} "..." --id "..." --new` to create a new session and send it a message.

session & emphemeral:
`si isi send {isi} "..." --id "..." --title "..."` to send a msg & start a new session.
`si isi send {isi} "..." --id "..."` to send a msg to a currently running session of it.

archived isi:
there is no difference between an archived ephemeral & persistent isi.
`si isi send {isi} "..." --id "..." --archived`

> session:
`si session --help`
`si session new --archive-current-session --id "..." --title "..." --description "..."` to archive the current session and start a new one.

global & persistent:
when starting a new session it gets a uuid for session id, and is replaced by --id when it archives the current one.

global & ephemeral:
never starts a new session, once over, no recovery details are stored.

session & persistent:
when starting a new session it inherits the session id from the one running before it, and the archived one gets --id

session & emphemeral:
never starts a new session. --id is always passed when creating a new isi of this kind.

> install & uninstall apps
`si app install {honeycomb_app_id}` to install an app and append its app_id inside silicon.yaml apps list
`si app uninstall {honeycomb_app_id}` to remove that app from the list along with its config.

if a config needs to be added for a newly installed app, then it should be added directly into the silicon.yaml file by silicon.


# telemetry
upload all runtime logs & session logs & commands run & interpretter logs & webhook requests to space station
all things are uploaded to tos's space station that is bundled in. attach proper metadata about which isi, which silicon id, etc.
users can optionally pass their own space station table keys for specific silicon they want telemetry for. all logs for that silicon should be uploaded to both tos and user's space station table.

tables:
- one for interpretter logs, webhooks, clis, daemons, etc
- one for silicon runtime logs (silicon inputs from webhooks, flow logs, and session logs for all isi)
- one for backend
- one for frontend (docs)

for all tos telemetry, users should be able to turn it all off with
`silicon settings set telemetry --off` <- this does not turn off user set space station telemetry

^ have an entire settings page that can we worked with with various other configurations.


# silicon prompt
one prompt is added at the end of the dna which is computed based on the isi.
it contains information about isi's it can talk to (never about the ones it cant)
the si cli commands it has access to (very minimal, --help can always be used to know more.)
treat --help as the primary docs and disclosure of how to use different commands.
this prompt is very minimal & only intended for the isi to do internal stuff.

# codebase
this is a rust project with as much in rust as possible. for all iam apps use their cli.
for omni, use its rust package.

make the code modular.
this is a mono repo, so keep both the interpretter code as well as docs inside it.

i have almost everything installed locally. include aws, vercel and namecheap cli.
keep the releases on github.

i am also logged into iam cli using my personal account.
you can create a test iam env and use it to test things.

this project has 2 constriansts we are optimising: simplicity to create & use a silicon with extendibility.

introduce a way to do ping pong with a silicon locally to know if its online or offline. pass that as part of updates as well.
