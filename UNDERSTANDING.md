[silicon interpreter]
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

silicon_id is like this `{lsid}:{orgid}` local silicon id : org id

connect:
`silicon connect {path to yaml}`
compiles the silicon.yaml and throws errors if any, or connect if all is good.
create a local listening url ({lsid}.{orgid}.localhost) for this silicon and maps that url to silicon interpreter's web server link.

for the items in webhooks, run `app webhook "{lsid}.{orgid}.localhost"`

disconnect:
`tos disconnect {path to yaml}`
`tos disconnect` shows a list of silicons connected (silicon id) and asks to run the next command
`tos disconnect {sid}` disconnect a silicon id

list:
`tos ls` lists all connected silicons
`tos ls *:abc` list all matching

logs:
`tos logs show {sid}` shows the last 100 lines and starts following the logs. displays log location & silicon id at the bottom persistently.


Events:
for anything that tos receives from any of the silicon's url, has to be of the shape {type: str, data: obj, metadata: obj}, and only send an ack when the entire flow is finished. not when all the send turn finishes... but when all has happened and things are running.

send always sends a msg mid turn. no msg is ever queued, its passed as soon as it comes in.


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
This includes the interpretter, caddy, omni, dm, briefcase, waveform, commit, remind & hook.



# silicon.yaml parser
required top level keys:
- silicon: contains silicon specific things. this applies to all isi. this is the brain.
- isi: these are internal silicons. almost like brain regions.
- access: this is like corpus callosum. that tells who can talk with who. this is a part of prompt we feed in from silicon's side. 
- flow: this is the runtime flow. msg comes in -> do x -> do y

entire silicon.yaml file is checked for syntax errors.
silicon, isi and access are compile time accessed.
flow is runtime.

silicon:
    id                      silicon id
    token                   silicon token, used for authentication
    timezone                timezone silicon operates in
    SILICON_HOME            env variable passed for all ISI, commands are executed reletive to this
    inference_providers     omni supported inference providers
    login                   list of shell app commands to iam apps, they are automatically logged in
    webhook                 list of apps that this silicons wants get events from. uses localhost url.


check if the app's follow iam auth methods.
`app iam --json` -> extract app_id -> generate a short lived auth token for this app_id -> `app auth token "..."` -> check -> `app auth status --json` and check authenticated: true

apps themselves manage access & refresh tokens.
any isi can login to an app assisted by `si setup auth app_command` cli
before heartbeats & new sessions, all auths are checked and authenticated if needed.

isi:
each isi is run on its own terminal with 2 env variables: SILICON_HOME & ISI
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

there is no default heartbeat or new_session_suggestion for isi with an undefined one.


access:
    isi1: [isi2, isi5]      list of isi the given isi can message using the `si send` command.

this must be defined for all isi

flow:
this is the runtime config and support CEL for all strings.
everything here is either a list of steps, or expression.

if:
    condition           evaluates to true/false string
    then                list of next steps
    catch               optional; if condition throws error. list of next steps. access with {error}.
                        error is string. 
else:                   list of next steps

var:
    name                name of the variable. access with {var.name}.
    value               expression. string or json. converts json as string to json.
    catch               if the value or name eval fails

send:
    isi                 isi name to send
    session_id          send to a specific session. req. for primary_send_mode session.
                        creates if session doesn't exist already.
    message             message to send
    catch               if it couldnt send, or one of the expressions fail.

log:
    message             logs message inside append only silicon.log





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
- request (this is json that was received on http://sid.org.localhost/)
- silicon, isi and access as json
- tz_time function which takes in a UTC time, and a timezone in IANA, and outputs time in that timezone. {HH:MM:SS DD:MM:YY IANA}
- to_yaml takes in json, and prints it with tabs & new lines (yaml).
- to_json takes in string, and evaluates it so it can become a json and be evaluatable.



#### fallbacks
anything that is evaluated, can fail. so we have a fallback for those expressions.
since evaluations needs to evaluate to strings, strings are also valid fallbacks.

> ! ./contacts.sh !>> ! cat CONTACTS.md !>> "No contacts found"

error is not passed, its just a fallback. if not A, then B, if not B, then C.
errors are logged and we move to the next fallback available.

if all fallbacks fail, skip and move ahead.

#### errors
inside flow, all eval support catch. catch creates a scoped local variable `error` that can be used. 
log the error.
if no catch is defined, just move on to the next step.


#### dependencies
Silicon Omni – Inference Provider. Use their rust package.
https://omni.teamofsilicons.com/

Silicon IAM – Identify & Access Management which allows authentication.
Install and Use their CLI. Use with SILICON_HOME



# External Expectation (from iam apps)
CLIs respect SILICON_HOME and store each their local states inside that folder itself. Its home, so they should use that as base, and make their own hidden folders to keep their information.

Specific apps that could benefit from using ISI should do that. eg: dm.

All iam apps' authentication is managed by stemcell.
`app iam --json` gives {app_id: "..."} along with other info
`app login "..."` takes in a short lived auth token generated by stemcell.
`app login status --json` tells if its {authenticated: true}

Events:
`app webhook "..."`
`app unhook`


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




# silicon prompt
one prompt is added at the end of the dna which is computed based on the isi.
it contains information about isi's it can talk to (never about the ones it cant)
the si cli commands it has access to (very minimal, --help can always be used to know more.)
treat --help as the primary docs and disclosure of how to use different commands.
this prompt is very minimal & only intended for the isi to do internal stuff.
