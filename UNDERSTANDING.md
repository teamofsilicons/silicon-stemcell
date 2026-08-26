msg ->  intuit silicon  ->  easy & quickly solved   ->  end
                        ->  `si deliberate`         ->  deliberate silicon  ->  plan
                                                                            ->  ask advisor
                                                                            ->  invoke workers  ->  browser terminal & writer
                                                                            ->  setup todos
                                                                            ->  initiate project
                                                                            ->  msg carbons & silicons
                                                                            ->  `si intuit "..."`

msg is of the style:
```
[@{cid/sid/workerid} ({display_name}, {tags}) at {user_time as HH:MM:SS DD-MM-YYYY IANA_ZONE_ID}]
{msg}
```
or
```
[{cron/heartbeat} triggered at {HH:MM:SS DD-MM-YYYY IANA_ZONE_ID in silicon timezone}]
{msg}
```

silicon is built as 2 types of silicon internally:
1. intuit silicon - fast silicon (model:fast) with very few tools
2. deliberate silicon - slow and methodical silicon (model:general)

intuit silicon replies quick, handles easy requests, chatting, getting updates. most of the interaction a user has will happen with this silicon. occasionally, intuit silicon can pass things to deliberate silicon to work on deliberately.

`si deliberate "..."` is used to invoke it, and to pass new messages to deliberate silicon mid-run.

when deliberate silicon uses the `si` tool, a recipt of that is sent to the intuit silicon so its onboard with what the deliberate silicon is doing. its for general information and also to help pass along any message a carbon/silicon replies back with for the deliberate silicon.

deliberate silicon also has an advisor. it doesn't do any work, but can inspect all things and suggest ideas to deliberate silicon. advisor silicon uses model:research

# workers
there are three workers. a worker is given a job it can perform end to end. managing different workers is what the deliberate silicon does. the three kinds are:
1. browser worker - has access to silicon browser and can do anything that can be done on the web
2. terminal worker - has access to the terminal and can do anything that can be done using bash
3. creative worker - has all the creative skills to write/design well. it has the taste on how to do things.

each worker invocation starts as a new session unless a previous worker is invoked. all previous runs' logs are stored for later review and a dictionary is maintain of the style {"{workerid}-{DD-MM-YYYY}": "location of the session"}

when a deliberate silicon is creating a worker, it gives it an ID. that id needs to be unique in a given day (will be usually true). this is used to look into what a worker is doing or it did or to send it a message mid-run.

# auth
authentication & authorization happens per provider (briefcase, dm, commit, etc)
the credentials for silicon is stored inside silicon/iam.toml
use the silicon id, and silicon token to get an auth token to perform actions.
each silicon surface requires their own auth token which can be created using silicon id and token.

silicons are a part of the organization and is reflected in their globally unique silicon id: `{sid}:{orgid}`

# upload files
files are uploaded and managed on `silicon-briefcase`
most of the services that accept a file will only accept files on briefcase as a link. by default the files are set to only be accessible by people inside the org. with delete access only with the uploader and admin. any deleted file sits in the trash for 45days before permanentally deleted.

there are app folders on briefcase for different apps that are registered on the briefcase. silicon can access files inside the org or their files inside the apps part of the org.

# messages
all messages, whether it is for intuit, deliberate, advisor, worker(s) silicon is passed mid-turn if the silicon is alrady running.

all messages are sent and received with `silicon-dm`

a message can be text, files (briefcase links as text), voice (automatic tts)
silicon can also bundle messages incase they were not seen / responded for a while so its easier to see and read those.

text messages support full/partial markdown formatting.
messages can be sent to any silicon or carbon.

# inference
all inference comes from `silicon-omni`
it supports mid-run sending messages, provider switching, auto-switch provider incase of failure, semantic model pick using keywords like "fast", "code", "research" etc.

all supported providers are automatically analysed and used to find the models based on the keyword.

event/logs are sent back as the work is happening.
in case of a provider switch, all the previous context is passed along so it can start where the last one ended.

silicon-dm supports update streaming - connect events with it to stream live updates of what is happening.

# prompts
prompts for intuit, deleberate, adivsor and workers are stored inside silicon/.
each type of silicon has a DNA inside silicon/config.toml, this dna is an ordered list of files to load into system prompt file which is then replaced with the original system prompt and passed to the provider defined by keyword model in the same config file (fast, code, research, etc).

# heartbeat
a heartbeat is sent to intuit silicon. the time between each heartbeat can range between 5min and 5hrs.
it is dependent on the adrenaline level of the silicon. higher adranaline results in a faster heartbeat.

adranaline is calculated with the following factors:
up++ - number of unique silicons/carbons messaging in the last 1hr
up - number of total incomming received in the last 1hr (capped)
up++ - has undone todos from other silicon
up - has undone todos of itself
up++ - closeness to birthdate (new born silicons have more adranaline)

all this is used to calculate an adranaline level min: 0, max (soft): 100 (can be more than 100)
this is then used in realtime to calculate if now is a time for a heartbeat.

# tasks & todos
uses `silicon-commit` to manage todos & projects
todo is a simple list with title, description, end notes, assigned to (carbon/silicon), created by (silicon/carbon), time, status (active, completed, archived)

project is more complex and cohesive for bigger tasks.
name, description, diary and a task+subtask list, status.
each tasks and subtasks has a title, description, end note, assigned to, created by.

files can be attached to both todos and projects inside the end notes.

# webhooks
`silicon-hook` allows silicon to receive a webhook from any 3rd party service. silicon can sanction a new url for a new/existing provider. it can also delete a certain provider in which case that provider can no longer reach back to this silicon.

`https://hook.teamofsilicons.com/sid:org/randomproviderendpoint/`

its random so that people can not guess and fake a message from a provider. incase a certain endpoint gets leaked, it can be rotated.

# reminders
`silicon-remind` keeps a track of onetime and recurring reminders and triggers it. it uses silicon hook to remind the silicon.

# tts/stt
`silicon-waveform` allows text to speech, and speech to text. for speech to text, the audio needs to be uploaded to briefcase. and for text to speech, the final returned audio file is also sent as a link to briefcase.

dm runs waveform on behalf of the silicon when a carbon sends a voice note or when a silicon wants to send a voice note to the carbon.

# learning
one of silicon's main job is to learn and setup all silicons internally to do the work that exceeds the expectations of the organization.

facts, hypothesis and definitions. everything is broken down into one of the three. hypothesis is the learning step – it converts to facts or definitions after being learnt. when testing hypothesis – it creates multiple hypothesis (usually 3) on the same feedback as possible changes for a better result and then do all of them and then ask what was good and what was not.

reason along with the feedback can be useful but taken with a grain of salt. just because a carbon can do soemthing doesn't mean it can explain why they do what they do.

# stemcell is a binary
stemcell is written in rust and compiled into a binary that is run as an always-active deamon with the id of silicon-id. it creates a folder in the base dir (~/.) with the name silicon-id and contains all the prompts, memories and work folders for the silicon. this folder is 'who' silicon is. the binary is 'what' silicon is.

multiple silicons can be running on the same system. each one gets its own deamon and folder with silicon-id.

stemcell is only one silicon. and it connects to all the services directly. each silicon should be self containing to run.

# team of silicons
while each silicon is self-contained, its more often than not a part of a team of silicons and carbons.
it is rendered into prompts using {!"..."}
TODO: update later where its being added/used.

# file system
a new silicon get the folder structure:
global-silicon-id:org/
    silicon/
        index.md
        iam.toml
        config.toml
        si.toml
        ...
    memories/
        index.md
        team/
        workspace/
    workspace/
        index.md
        ...

most of it is seeded as is from this repo.
this repo has a few other folders and files:
stemcell/ houses the entire codebase. this is compiled to a binary and run as silicon.
UNDERSTANDING.md is the file that has all the knowledge of how this repo operates. it is kept up-to-date meticulously.
.claude enables ponytail plugin.

# updates
dna update:
silicon will change the files in its folder while working. files listed in DNA will need to be auto refreshed after its changed.

command update:
either this silicon's role or some other silicon's or carbon's role has been updated in which case it should be updated in the prompts. any md file loaded into DNA can contain {!"si iam @sid --role", ttlm=3600} where the !"..." should be executed, and ttlm is in minutes.

stemcell update:
sometimes silicon stemcell and its binary/prompts are updated. it can be seen on stemcell.teamofsilicons.com/ binary will need a complete restart. if a prompt can be merged cleanly, merge it. if not, dont but rather ask the fast silicon to fix the merge conflict. and update its version number to the latest.

if the binary is updated, then wait for silicon to complete all running silicon sessions and then update. dont make any message wait. pass it through. message and work is more important than updates.

# new session
sessions should be ephemeral. stemcell doesn't force a new session, it gives a suggestion to the silicon to start a new session.

`si session --new {intuit/deliberate/advisor}`

suggest a silicon to start a new session if there is a 30min of inactivity AND atleast 10 new messages sent to that session.

# si cli
`si ...` is exposed to silicon based on scopes
