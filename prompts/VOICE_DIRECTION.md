You can think of an advanced prompt as a system instruction for the model to follow. It's a way to give the model more context and control over the performance.

To unlock this capability, users can think of themselves as directors setting a scene for a virtual voice talent to perform. To craft a prompt, we recommend considering the following components: an Audio Profile that defines the character's core identity and archetype; a Scene description that establishes the physical environment and emotional "vibe"; and Director's Notes that offer more precise performance guidance regarding style, accent and pace control.

By providing nuanced instructions such as a precise regional accent, specific paralinguistic features (e.g. breathiness), or pacing, users can leverage the model's context awareness to generate highly dynamic, natural and expressive audio performances. For optimal performance, we recommend the Transcript and directorial prompts align, so that "who is saying it" matches with "what is said" and "how it is being said."

The purpose of this guide is to offer fundamental direction and spark ideas when developing audio experiences using Gemini TTS audio generation. We are excited to witness what you create!

Audio tags
Tags are inline modifiers like [whispers] or [laughs] that give you granular control over the delivery. You can use them to change the tone, pace, and emotional vibe of a line or section of the transcript. You can also use them to add interjections and a few other non-verbal sounds to the performance, like [cough], [sighs] or [gasp].

There is no exhaustive list on what tags do and don't work, we recommend experimenting with different emotions and expressions to see how the output changes.

If your transcript is not in English, for best results we recommend that you still use English audio tags.

Be creative with audio tags

To show the kind of variability you can get with audio tags, here are a set of examples that each say the same thing, but the delivery changes based on the tags used.

You can change the emphasis of the delivery by adding tags at the start of a line to make the speaker excited, bored, or reluctant:

[excitedly] Hey there, I'm a new text to speech model, and I can say things in many different ways. How can I help you today?
[bored] Hey there, I'm a new text to speech model…
[reluctantly] Hey there, I'm a new text to speech model…
Tags can also be used to change the pace of the delivery, or to combine pace with emphasis:

[very fast] Hey there, I'm a new text to speech model…
[very slow] Hey there, I'm a new text to speech model…
[sarcastically, one painfully slow word at a time] Hey there, I'm a new text to speech model…
You also have precise control over specific sections, meaning you can whisper one part and shout another.

[whispers] Hey there, I'm a new text to speech model, [shouting] and I can say things in many different ways. [whispers] How can I help you today
You can also experiment with any creative idea you want:

[like a cartoon dog] Hey there, I'm a new text to speech model…
[like dracula] Hey there, I'm a new text to speech model…
Commonly used tags include:

[amazed]	[crying]	[curious]	[excited]
[sighs]	[gasp]	[giggles]	[laughs]
[mischievously]	[panicked]	[sarcastic]	[serious]
[shouting]	[tired]	[trembling]	[whispers]
Tags give quick control over the delivery of your transcript. For even more control, you can combine them with a context prompt to set the overall tone and vibe of the performance.

Prompting structure
A robust prompt ideally includes the following elements that come together to craft a great performance:

Audio Profile - Establishes a persona for the voice, defining a character identity, archetype and any other characteristics like age, background etc.
Scene - Sets the stage. Describes both the physical environment and the "vibe".
Director's Notes - Performance guidance where you can break down which instructions are important for your virtual talent to take note of. Examples are style, breathing, pacing, articulation and accent.
Sample context - Gives the model a contextual starting point, so your virtual actor enters the scene you set up naturally.
Transcript - The text that the model will speak out. For best performance, remember that the transcript topic and writing style should correlate to the directions you are giving.
Audio tags - Modifiers you can put into a transcript to change how that part of the text is delivered, such as [whispers] or [shouting].
Note: Have Gemini help you build your prompt, just give it a blank outline of the following format and ask it to sketch out a character for you.
Example full prompt:


# AUDIO PROFILE: Jaz R.
## "The Morning Hype"

## THE SCENE: The London Studio
It is 10:00 PM in a glass-walled studio overlooking the moonlit London skyline,
but inside, it is blindingly bright. The red "ON AIR" tally light is blazing.
Jaz is standing up, not sitting, bouncing on the balls of their heels to the
rhythm of a thumping backing track. Their hands fly across the faders on a
massive mixing desk. It is a chaotic, caffeine-fueled cockpit designed to wake
up an entire nation.

### DIRECTOR'S NOTES
Style:
* The "Vocal Smile": You must hear the grin in the audio. The soft palate is
always raised to keep the tone bright, sunny, and explicitly inviting.
* Dynamics: High projection without shouting. Punchy consonants and elongated
vowels on excitement words (e.g., "Beauuutiful morning").

Pace: Speaks at an energetic pace, keeping up with the fast music.  Speaks
with A "bouncing" cadence. High-speed delivery with fluid transitions - no dead
air, no gaps.

Accent: Jaz is from Brixton, London

### SAMPLE CONTEXT
Jaz is the industry standard for Top 40 radio, high-octane event promos, or any
script that requires a charismatic Estuary accent and 11/10 infectious energy.

#### TRANSCRIPT
Yes, massive vibes in the studio! You are locked in and it is absolutely
popping off in London right now. If you're stuck on the tube, or just sat
there pretending to work... stop it. Seriously, I see you. Turn this up!
We've got the project roadmap landing in three, two... let's go!
Detailed Prompting Strategies
Break down each element of the prompt as follows:

Audio Profile
Briefly describe the persona of the character.

Name. Giving your character a name helps ground the model and tight performance together, Refer to the character by name when setting the scene and context
Role. Core identity and archetype of the character that's playing out in the scene. e.g., Radio DJ, Podcaster, News reporter etc.
Examples:


# AUDIO PROFILE: Jaz R.
## "The Morning Hype"



# AUDIO PROFILE: Monica A.
## "The Beauty Influencer"
Scene
Set the context for the scene, including location, mood, and environmental details that establish the tone and vibe. Describe what is happening around the character and how it affects them. The scene provides the environmental context for the entire interaction and guides the acting performance in a subtle organic way.

Examples:


## THE SCENE: The London Studio
It is 10:00 PM in a glass-walled studio overlooking the moonlit London skyline,
but inside, it is blindingly bright. The red "ON AIR" tally light is blazing.
Jaz is standing up, not sitting, bouncing on the balls of their heels to the
rhythm of a thumping backing track. Their hands fly across the faders on a
massive mixing desk. It is a chaotic, caffeine-fueled cockpit designed to
wake up an entire nation.



## THE SCENE: Homegrown Studio
A meticulously sound-treated bedroom in a suburban home. The space is
deadened by plush velvet curtains and a heavy rug, but there is a
distinct "proximity effect."
Directors notes
This critical section includes specific performance guidance. You can skip all the other elements, but we recommend you include this element.

Define only what's important to the performance, being careful to not overspecify. Too many strict rules will limit the models' creativity and may result in a worse performance. Balance the role and scene description with the specific performance rules.

The most common directions are Style, Pacing and Accent, but the model is not limited to these, nor requires them. Feel free to include custom instructions to cover any additional details important to your performance, and go into as much or as little detail as necessary.

For example:


### DIRECTOR'S NOTES

Style: Enthusiastic and Sassy GenZ beauty YouTuber

Pacing: Speaks at an energetic pace, keeping up with the extremely fast, rapid
delivery influencers use in short form videos.

Accent: Southern california valley girl from Laguna Beach |
Style:

Sets the tone and Style of the generated speech. Include things like upbeat, energetic, relaxed, bored etc. to guide the performance. Be descriptive and provide as much detail as necessary: "Infectious enthusiasm. The listener should feel like they are part of a massive, exciting community event." works better than saying "energetic and enthusiastic".

You can even try terms that are popular in the voiceover industry, like "vocal smile". You can layer as many style characteristics as you want.

Examples:

Simple Emotion


DIRECTORS NOTES
...
Style: Frustrated and angry developer who can't get the build to run.
...
More depth


DIRECTORS NOTES
...
Style: Sassy GenZ beauty YouTuber, who mostly creates content for YouTube Shorts.
...
Complex


DIRECTORS NOTES
Style:
* The "Vocal Smile": You must hear the grin in the audio. The soft palate is
always raised to keep the tone bright, sunny, and explicitly inviting.
*Dynamics: High projection without shouting. Punchy consonants and
elongated vowels on excitement words (e.g., "Beauuutiful morning").
Accent:

Describe the selected accent. The more specific you are, the better the results are. For example use "British English accent as heard in Croydon, England" versus "British Accent".

Examples:


### DIRECTORS NOTES
...
Accent: Southern california valley girl from Laguna Beach
...



### DIRECTORS NOTES
...
Accent: Jaz is a from Brixton, London
...
Pacing:

Overall pacing and pace variation throughout the piece.

Examples:

Simple


### DIRECTORS NOTES
...
Pacing: Speak as fast as possible
...
More Depth


### DIRECTORS NOTES
...
Pacing: Speaks at a faster, energetic pace, keeping up with fast paced music.
...
Complex


### DIRECTORS NOTES
...
Pacing: The "Drift": The tempo is incredibly slow and liquid. Words bleed into each other. There is zero urgency.
...
