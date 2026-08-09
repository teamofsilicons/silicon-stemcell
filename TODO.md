add a heartbeat for every manager that beats every 13min. every 13min, every manager is pinged and sent a message "congrats, your heart is beating, make it count!"

there are 2 stages when a message comes in from anyone.


`iwantto` cli tool for silicons (managers and workers) to interact.
All message text support full markdown syntax
if something in the iwantto cli reference as i have written it is not technically implementable as is, make small changes to it. could be things like what's mentioned inside quotes and all. try to stay as consistant across all cli possibilities.

say my manager send a message to a carbon that is not yet talking to this silicon, then the message will not know which manager to send it to. in that case, initiate that carbon's manager, and then send them the message is sent. then it is the manager's choice to pass it forward or to keep it. if its the first time its being sent, attach "you are not yet talking to your carbon, but this is the message that carbon id wants to send to your carbon, its advised to pass it forward.".

msgid is event id of the msg.

replace feature for all unread messages is basically takeback.

with each heartbeat, send a list of active work initiated by this manager. `iwantto work --active --by {carbonid/siliconid}`

when starting a new session, a message can be passed from the old session to be sent as the first msg when a new session is started. 

we are adding a new type of agent to silicon called Advisor. there is an advisor once per manager and its sole purpose is to give advice to a manager on how to think.



msg trigger -> setup questions -> work -> done


# Setup questions
setup questions is a tree based questionaire to be asnwered before working anytime a manager is triggered.
give the options when the options are available. and tell them when its an open answer. keep asking question in rounds (and mention the chain for each question they are still on). Check and change the structure of the questions if needed. Running commands during this phase is not allowed. This phase is meant to think and remind of the things to do.

# Advisor
this is a new addition to the system.
current structure:
carbon
|
manager – advisor
|
3 workers

each manager gets an advisor to help them think through things.

Advisors use the same model provider as the manager, and get the following files in their DNA context (in order):
1. INDEX.md
2. IWANTTO_CLI_REFERENCE.md
3. ADVISOR.md
<question from the the manager>

and then similar to what we do for workers, give the final output back as the result of the run.
start a new advisor session if there is a gap of 2 hours between the last advisor invocation and the current one. OR when its been more than 24hrs of the same advisor session.

Advisors also have a heartbeat every 5 hour. And in this case, this is what is sent "Your manager did not trigger you, This is a heartbeat. Check on your manager's work and give any advice you want." And in this case, the output of the Advisor is sent to the manager just like a worker's output (as a message marked to be from the advisor).

`iwantto get-advice "..."` is syncronous. When asked, the advisor is invoked, it works and the commands keep on-hold until the advisor is done and returns the advice. this is so that the advice can be acted upon.

advisors get the same iwantto cli as the manager. technically an advisor can do anything as if its the manager. but it wont. it uses these commands to get context and reply back with advice.

## iwantto cli
for a manager and advisor, they share the same "i". what i mean by "i" is that in iwantto, "i" needs to be resolved as in who is running this command. each command is run relative to the manager/advisor running it.

## diagnosis
keep a store of all the things that happen inside the silicon. all manager invocations, all commands it runs, all messages it sends, all files it writes. this will be useful to debug the system when testing.

## how to code
- i have made a LOT of changes. things like ADVERTISING was a folder before, it is not now. it is just a simple file.
- i am assuming that TEAM_OF_SILICONS.md gets the Advertising memory of all other silicons in the team.
- iwantto cli reference cli is the most imp file and the only final file. if you notice any descripency – then update it in accordance to the iwantto cli reference.
- iwantto cli should be implementable as i have mentioned it already, but if something needs to change like adding or removing quotes and all, do that. minor changes are allowed.
- if you write any prompts that will be passed to silicon, tell me what you wrote.
- and write good, maintainable and simple code.
- don't change the prompts i have written (esp the ones i have written just now)

the key objective with the iwantto cli is that silicon manager can now do things mid-run and wont need to wait to give json tools at the end of the run.