---
name: setup
description: Sets up the job matcher for a new user. Asks for their resume, reads it into a job-search profile, confirms where and what they want to work on, builds a list of company job boards to watch, and schedules a weekday job match with a results dashboard. Also changes an existing setup. Use when the user says "set up job matcher", "get started", "here's my resume, find me jobs", "change my job search", "update my resume", "pause my job matcher" or "change when it runs".
---

# Job matcher setup

Goal: in one short conversation, go from a resume to a scheduled daily job match. Keep the user-facing side plain and quick: no file paths, JSON or tool names in what they read.

Scripts: `JM` means `python3 <run skill base directory>/scripts/jm.py`. The run skill sits next to this one (`../run/` from this skill's base directory).

Reference files in this skill: `references/profile-schema.md` (every profile field and how to fill it) and `references/scheduled-prompt.md` (the scheduled task's prompt template).

## Step 0: Existing setup?

Load the scheduling tools with ToolSearch (`scheduled task list create update`) and look for this plugin's task: named `Job matcher` AND with `<job-matcher-profile>` in its prompt. Ignore every other task, even one with a similar name: it belongs to something else and must never be changed.

- **Found:** this is a change, not a fresh setup. Take the profile, boards and resume blocks from its prompt, ask what they want to change (or do what they already asked: new resume, different places, new titles, different time, pause, resume, delete), apply it, and update that same task in place. For a new resume, redo Steps 1 to 3 and keep their earlier answers as defaults. Confirm the change in one or two sentences and stop.
- **Not found:** continue.
- **No scheduling tools in this session** (for example a terminal session): say the daily schedule needs the Claude app (web or desktop), then still build the profile and boards (Steps 1 to 5) so they can run `/job-matcher:run` by hand, and offer to finish the schedule from the app later.

## Step 1: Get the resume

If a resume is already attached or pasted in this conversation, use it. Otherwise ask, in one message, and wait:

> To get started I need your resume. Attach it here (PDF, Word or text) or paste the text. It stays in your Claude account: it's saved inside your own scheduled task and used only to score jobs.

Extract the text with contact details removed: `JM resume-text --redact <file>` (for pasted text, save it to a .txt file first). If that fails (for example a scanned PDF), read the file directly and remove the email, phone number, links and street address yourself. If the text is still unreadable, ask for a text or Word version. Use only the redacted text from here on.

## Step 2: Read it into a profile

Build the profile described in `references/profile-schema.md` from the resume alone. Be accurate rather than flattering: years in the target role, degree status and management experience drive the scoring caps, so take them exactly as the resume states them. Never invent skills.

Then show the user a short summary (5 to 7 lines, plain words): the titles you would search for, their level and years, home location, top skills, and anything you were unsure about. Ask them to correct anything that's wrong as part of the next step.

## Step 3: Confirm preferences

Ask with AskUserQuestion in one call (up to 4 questions, each standing on its own; offer the likeliest answer first):

1. **Roles** (multi-select): 3 or 4 titles drawn from the resume, best fit first.
2. **Where** (multi-select): "Remote, anywhere in <country>", "Hybrid or onsite near <home city>", "Open to relocating", plus a fourth if the resume suggests one.
3. **When** (single): "Weekday mornings (Recommended)", "Every morning", "Monday, Wednesday, Friday".
4. **Avoid** (multi-select): "Take-home assignments", "Recorded video interviews", "Roles managing people", "Specific companies (type them in)".

Follow up in plain text only for what the answers leave open: relocation cities if they chose relocating, company names if they chose specific companies, and corrections to the summary. Keep follow-ups to one message.

Fill `locations`, `preferences`, `excluded_companies`, `filters` and `schedule` from the answers, and write the finished profile to `profile.json` in a scratch folder in the session's working directory (the resume text goes next to it as `resume.txt`). For the schedule, get the user's time zone (from a current-time tool if the session has one, otherwise ask) and pick a time between 7:00 and 9:00 local that is not on the hour or half hour, for example `CRON_TZ=America/Chicago 41 7 * * 1-5`.

## Step 4: Build the board list

Most employers post through Greenhouse, Ashby or Lever. Build this user's list of boards to watch:

1. Write 50 to 70 employer names likely to hire their titles in their places: companies in their domains and close neighbors, well-known employers in their home metro, and remote-friendly companies for their function. Add employers seen hiring right now from 3 or 4 WebSearch queries (`"<top title>" site:jobs.ashbyhq.com`, `site:job-boards.greenhouse.io`, `site:jobs.lever.co`, with their city or "remote"); the slug is in each result's URL.
2. Remove anything in `excluded_companies`.
3. Probe them: `JM probe "<name>" "<name>" ...` returns the live boards and their job counts. If it reports a network error, check slugs with WebFetch on the board URLs from the run skill's `references/sources.md` instead (lowercase name with no spaces is the usual slug).
4. Keep boards with at least one open job. Aim for 30 or more; if fewer than 15 turned up, add another batch of names and probe once more.
5. Write them to `boards.json` in the format at the end of `references/profile-schema.md`, with each employer's display name.

The board list grows on its own afterwards: each run's discovery searches add new boards to the dashboard's memory.

## Step 5: Create the dashboard page

Run `JM empty --profile profile.json --out dashboard.html`, then publish it as a new artifact titled exactly `Job matches` (icon `briefcase`) with the artifact tools (load with ToolSearch `publish artifact`). Put the returned slug in `profile.dashboard.slug`. If the artifact tools are unavailable, leave the slug null; the first run will create the page.

## Step 6: Schedule it

Fill the template in `references/scheduled-prompt.md` and create one scheduled task named `Job matcher` with the cron from Step 3. With a cloud scheduling tool (such as `create_trigger`): the run needs no computer access, so do not require the local device or attach folders; turn on push notifications. Say in one sentence which approval setting the task got. If its runs will stop to ask for approval, tell the user they can switch the task to automatic approval in its settings, since nobody will be there to click approve at 7 AM.

## Step 7: First run and wrap-up

Tell the user setup is done and that the first match is starting now (it takes a few minutes), then load the run skill and run it with the profile, boards and resume you just built. When it finishes, close with:

- when it will run next, in plain words,
- that the dashboard link is the place to check each morning (roles never repeat, and "Mark applied" and "Hide" remember their clicks in that browser),
- that they can say "change my job search" any time to adjust titles, places, the schedule or the resume.

## Rules

- Never store the resume or profile anywhere except the user's own scheduled task and dashboard page. Do not save them to memory unless the user asks.
- Never apply to jobs, sign in to job sites, or contact anyone.
- Keep contact details out: no email, phone number, links or street address in the stored resume text or the profile, and only the first name in the profile.
