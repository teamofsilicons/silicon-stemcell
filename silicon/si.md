# this is how `si ...` commands are configured along with the scopes per silicon.

silicon.intuit = [iam.read,]
silicon.deliberate = [iam.read,iam.edit,]
silicon.advisor = [iam.read]
silicon.worker.browser = []
silicon.worker.terminal = []
silicon.worker.creative = []


[iam]
`si iam --help`

read:
`si iam @{cid/sid}` -> name, id, role, org, tags, trust
`si iam @{cid/sid} --role`, also available, --name, --id, --org, --tags
`si iam --tags` -> read all available tags in this org.

edit:
only role and tags are editable
`si iam @{cid/sid} --role --edit "..."` changes the role text.
`si iam @{cid/sid} --tags --edit "..."` raises a tag change request to the user & admin.


[dm]
`si dm --help`

read:
`si dm @{cid/sid} --filter "between:DD-MM-YYYY=DD-MM-YYYY -> last:10 -> is:read -> contains:'confirm*', has:attachment"` will first get msg between those dates (both inclusive) and strip it down to last 10 msgs, keep only read msges, then check if contains the word starting with confirm or if it contains an attachment.

TODO: We'll need to develop an entire set of operations silicon can use for filtering along with the docs of how to use it.

`si dm inbox --limit 15` by default with a limit of 10. this shows the last 5 messages of the most recent 10 contacts of the given silicon.

write:
`si dm @{cid/sid} send "..."` send a text msg, files (briefcase links), or voice (just send a single breifcase link to the audio) to a carbon/silicon. dm does not support file uploads, or voice directly. it will show attachments that are on briefcase.

`si dm @{cid/sid} bundle [msgid1,msgid2,msgid3,msgid5,msgid10] "..."`

[commit]
`si commit --help`

todo:
1. get M done
2. finish N
3. ensure O (assigned to X)
4. Complete Project: "Find the ICP"

project:
Name: Find the ICP
ID: find-the-icp-randombits
Description: ...
Status: Ongoing (1/3 tasks completed)
Tasks:
    Get A done (done)
        Find out about A.1 (done)
        Analyse A.2 (assigned to G) (done)
    Check B (assigned to H)
        Check null hypothesis
        Is B.1 significant
    Ensure C
Diary: ...

while both todo and (tasks, subtasks) take description, and end notes. project has a project diary where you can take ongoing. A project is automatically added to the todo of the silicon that created it.

read:
`si commit @{cid/sid} --filter "is:active"` lists all active todos and projects for this silicon/carbon.
`si commit @{cid/sid} --filter "is:active -> is:todo"` or is:project for getting only active projects
`si commit @{cid/sid} --filter "is:completed -> is:project -> between:DD-MM-YYYY=DD-MM-YYYY"` both inclusive.
`si commit --id "todoid/projectid"`

edit:
`si commit @{cid/sid} --todo --add `


[hook]

[briefcase]

[waveform]

[remind]

[advisor]

[intuit]

[deliberate]

[worker]

[session]

[]
