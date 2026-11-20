# job-matcher

Give Claude your resume once and get a dashboard of fresh, relevant job openings every weekday. Each role is read in full and scored 1 to 10 against your resume, with the line from the posting behind every point. Roles never repeat.

**Install:** in Claude, open Customize > Plugins > Add > Add marketplace and enter `connorabuilds/claude-job-matcher`. In Claude Code:

```
/plugin install job-matcher --marketplace connorabuilds/claude-job-matcher
```

**Start:** type `/job-matcher:setup` (or say "set up job matcher") and attach your resume. Setup takes about three minutes and runs your first match.

**Skills**
- `setup`: resume to profile to weekday schedule; also changes an existing setup ("change my job search").
- `run`: the daily match. Runs on the schedule, or say "run my job matcher".
- `score-job`: paste a job link or description and get a scored breakdown.

**Needs:** a paid Claude plan with scheduled tasks. No other accounts. Uses public Greenhouse, Ashby and Lever job boards plus web search; uses Indeed or ZipRecruiter connectors only if you already have them on.

**Privacy:** your resume (with contact details removed) and profile are stored only in your own scheduled task and dashboard page. The plugin never applies, signs in to job sites, or contacts anyone.

Full documentation: https://github.com/connorabuilds/claude-job-matcher
