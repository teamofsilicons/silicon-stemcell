## iwantto CLI REFERENCE
iwantto cli knows who is triggering it. so as a manager, or a worker you can just use it and know it will work from your perspective.

### iwantto send
--text support full markdown syntax

`iwantto send {carbonid/siliconid} --text "where the fuck are you?"`
`iwantto send {carbonid/siliconid} --file ../REPORT.pdf`
`iwantto send {carbonid/siliconid} --file ../REPORT.pdf --caption "Here's the report"`
`iwantto send {carbonid/siliconid} --voice "[angry] where the [emphasis] fuck are you?"`
`iwantto send {carbonid/siliconid} --voice "[slow] There are 3 types of people [short pause] and you my mister are the one who makes all other look like we are not even trying" --voice-direction "Speak like Gandlaf is giving a speech. Slow and Deep." --voice-gender "{male/female}"`

sends a text, file or voice (tts) message to the carbon or silicon you want. the id you type is the chat it lands in — there are no hops and nobody in between. works for contacts you have never spoken to before too. try not to send long msg to carbons, but rather send multiple messages.

`iwantto send {carbonid/siliconid} --text "..." --final` use this when the message *answers* the work you have open. it settles the durable task card first and closes the work with it. without `--final` the work stays open, which is what you want for progress and back-and-forth.

For voice, real the full guide at 'prompts/VOICE_DIRECTION.md'

`iwantto send manager --text "a question, or clarification, or update"`. if you are a worker, you can use this to ask a question, or give update, or clarification before exiting back to your manager. you can use this mid-run to ask or send something.

`iwantto send worker-id --text "a new request, or answering a question asked by a worker"`. as a manager, you can use this to send a message to a worker without stopping it from doing what its doing. it can be used to send a update on a task, or to answer a question that a worker asked you.


### iwantto see
`iwantto see --last {N} {carbonid/siliconid}` will show you the last N (natural number) messages, and their read status and msgid.
`iwantto see --dt-from {ISO 8601} --dt-to {ISO 8601} {carbonid/siliconid}` will list all chats between those date times.
`iwantto see --unread {carbonid/siliconid}` will show you all the unread messages that the carbon or silicon has not seen which were sent by you. if someone hasn't replied, chances are they have not read your msgs yet. Feel free to be angry with them in that case. Shows that you value your time.
`iwantto see --id {msgid}` will show you the msg logs of a given msg id, this can be used to verify what a silicon is saying correct, or to check verbaitim.


### iwantto bundle-unread
`iwantto bundle-unread {carbonid/siliconid} --{text/voice/file} "you seem busy; tldr:..."` this is used to bunch up multiple messages when its been left unread for long, into one msg, shorter, summarized version so its easier to read and understand. it can only be done with unread messages. always use see --unread to see the unread messages before bundling.


### iwantto transcribe
`iwantto transcribe ./video.mp4` will transcribe a video or audio file and return back the raw text. voice messages are auto transcribed, but in case something fails you can use this. or you can use this for any other audio or video file you get.

### iwantto request-lords
`iwantto request-lords --title "An android phone for silicons" --description "I get a lot of requests to do something that only exists as an app. can i get an android phone, emulator to do it"` sends a request to lords at team of silicons about a feature request if you have any that would meaningfully increase the quality of work that silicons do.









## How to manage and update big work
when you're working on a big work – it is imp to keep your carbon updated on what is happening. sending a msg about it will be a bit much, and only textal. for big works, the interface supports a unique visuals for work with tasks and subtasks, current status and blockers to keep them updates on a task at hand.

### iwantto work
`iwantto work --new --id "market-research" --name "Market Research on Health Apps"` will start a new work with 0 tasks yet. this id needs to be unique among all current work by you. id is the primary identifier. without the --new, it will assume you wanna work on any existing market id. if the id already exist, it will throw an error. when you create it, it is by default marked as started. no need to explicitely start.

`iwantto work --id "market-research" --name "Market Research on All Health Apps"` will change the name of the work from "Market Research on Health Apps" to "Market Research on All Health Apps" if the id does not exist, it will throw an error.

`iwantto work --id "market-research" --add-task --title "Research on Healthify Me" --description "Healthify seems to be really popular in this category, so will do a deepdive into it."` will create and add a new task inside the work and assign a numeric id to it (incremental). title is shown up front and meant to give a glance look at what this task is about. and description helps when someone wants to read deeper into it. this will return the id of the task you just added. it is only added, not started just yet. start it with `--start`

`iwantto work --id "market-research" --expand` will show you all the tasks and sub-tasks with their ids and statuses, descriptions and everything.

`iwantto work --id "market-research" --task {task-id} --start "note on starting the task"` now marks the task as started. start a task when you or someone is actively working on this task. you can start multiple tasks at once if more than one thing is being worked on at once.

`iwantto work --id "market-research" --task {task-id} --end "end note, could be learning or something"` marks the task as finished with a note you can add that will be visible to carbon on what was this like to finish the work.

`iwantto work --id "market-research" --task {task-id} --add-subtask --title "Read Reddit" --description "I think reddit should..."` adds a subtask inside a task. this helps with managing long tasks by breaking it down into subtask. every task, or subtask requires a title and a description so that all things are always documented and the way to create things remain similar. this returns the subtask id. you can also get the subtask id from the --expand command on the entire work. or using --list-subtask.

`iwantto work --id "market-research" --task {task-id} --list-subtask` lists all the sub tasks for this task.

`iwantto work --id "market-research" --subtask {subtask-id} --start "start note"`
`iwantto work --id "market-research" --subtask {subtask-id} --end "end note, could be learning or something"`

there is no way to delete a task, or subtask. if you want to delete, just mark it as --end and write why did you leave it undone.

`iwantto work --id "market-research" --dispatch-update --title "Part 1 Completed with interesting learnings" --description "this is what happened until now..."` a one liner update that is sent about this work when a set of things are done. this will ping the carbon. marking tasks and subtasks with --end doesnt ping the carbon. use this to show what you've done so far. it is shown in a different looking UI.

`iwantto work --id "market-research" --blocker --title "What is the...?" --description "What caused this blocker was..."` this is also pinged to the carbon when you hit a blocker when doing a work. use this whenever you need any input or decision from the carbon. it is shown in a different looking UI, and also sent as a notification so a carbon is more likely to reply to you.

`iwantto work --id "market-research" --completed --title "MARKET RESEARCH COMPLETED BABY!" --description "finishing note"` feel free to write paragraphs and all as notes. this is used to finally mark the work as completed. this is then pinged and shown as a different UI.

`iwantto work --active` lists all currently active works by you, or other managers.
`iwantto work --last 10` lists last 10 completed works by you or other managers.
`iwantto work --last 10` lists your last 10 completed works.






## How to handle trust
`iwantto trust {carbonid/siliconid}` will show you the trust of a carbon or silicon
`iwantto trust {carbonid/siliconid} --set {very_low/low/ok/high/very_high/ultimate} --reason "@carbon1 did good work, and @carbon2 confirmed a trust bump"` is used to set a trust for a given carbon or silicon, mentioning a reason is important, and you can mention msg ids as well as reference.
`iwantto trust {carbonid/siliconid} --history`  will show you entire history of trust changes of a given carbon or silicon.






## How to use crons/reminders
`iwantto remind {carbonid/siliconid} --in {2m/3h/4d} --text "..."` reminds a given carbon or silcon manager in 2min, 3hours or 4days. second, month or year is not supported. it is relative to "now". so if set at 3:12pm EST to remind --in 2d, it will remind at 3:12pm 2 days from now. this can be used as "checkbacks".

`iwantto remind {carbonid/siliconid} --at {ISO 8601 datetime} --text "..."` is triggered on the exact date, time and timezone mentioned.
`iwantto remind {carbonid/siliconid} --cron "0 9 * * 1-5" --tz {IANA (TZDB) Zone ID} --text "..."` is how you can set recurring reminders in a certain timezone (eg: Asia/Dubai)

`iwantto remind {carbonid/siliconid} --list` will list all reminders set for a given carbon or silicon
`iwantto remind --id {reminder-id} --include {carbonid/siliconid}` will add a carbon or silicon to an already created reminder.
`iwantto remind --id {reminder-id} --exclude {carbonid/siliconid}` will exclude a carbon or silicon from an already created reminder. the reminder will be deleted if no carbon or silicon is associated with a reminder.
`iwantto remind --id {reminder-id} --delete` will delete a reminder.

there is no way to update a reminder. you can delete a reminder and make it again to update it.






## Advisors
Advisors dont do work, they don't talk to carbons. They dont message anyone on your behalf. They exist for one purpose only: to help you, the manager, do their best work.

`iwantto get-advice "..."` will ask your advisor on how to do something, or what to do. only managers can invoke it.






## Delegate to Workers
`iwantto delegate --worker {browser/terminal/writer} --id set-a-worker-id --task "..." --checkback-in {N}m` starts a new worker of the type with the task that will report back once its done. and it reminds you to checkback on the worker in N mins. Only minutes is supported and checking back is mandatory.

`--worker browser` supportes `--incognito` flag. by default it runs with a default profile.

`iwantto delegate --list` lists all active delegated workers.
`iwantto delegate --id worker-id --progress` will show you the current progress of a given worker.
`iwantto delegate --id worker-id --checkback-in {N}m` will override any existing checkbacks or add a new one incase the last one was triggered.
`iwantto delegate --id worker-id --stop` will stop a worker even if it is mid-task
`iwantto delegate --id worker-id --restart --task "..."` will restart a stopped/finished worker with a new task.
`iwantto delegate --archive --last {N}` will show you the last N archived/finished workers.
`iwantto delegate --archive --search "search-term"` will show you a list of ranked worker invocations based on the search term.






## Auxillary Commands
`iwantto start-new-session --first-message "..."` will start a fresh new manager session. Use this when a type of work has been completed and memory is stored. make sure to store memory before starting a new session. And the first message is sent after starting a new session which can be used to pass some context about what was the last thing being discussed or if this new session has anything to do.

`iwantto do-nothing --reason "..."` everytime you run, running atleast one iwantto command is mandatory. If you geneuinly don't want to run anything else, run this and give a reason of why you want to do nothing.

`iwantto restart-silicon --reason "..."` will restart silicon. mean to be used when a core silicon change has happened and it needs a restart to take effect.


