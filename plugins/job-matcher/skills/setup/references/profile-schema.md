# Profile format

The profile is a JSON object built from the user's resume and answers during setup. It is stored inside the user's own scheduled task (see `scheduled-prompt.md`), never anywhere else. `examples/sample-profile.json` in the repository shows a complete fictional one.

| Field | Type | How to fill it |
|---|---|---|
| `version` | number | Always `1`. |
| `display_name` | string | The user's first name only, for the dashboard heading. Empty string if they prefer. |
| `headline` | string | One line summarizing the resume, in plain words. |
| `target_titles` | list | Job titles to search for, best first. Start from the resume's most recent roles and the direction the user confirms. 3 to 8 entries. |
| `seniority` | string | `entry`, `mid`, `senior` or `lead`, judged from years and scope on the resume. |
| `years_experience_total` | number | Total professional years on the resume. |
| `years_in_target_role` | number | Years with the target job title or its direct equivalent. Often much lower than the total; be honest, since this drives the years cap. |
| `education.highest_degree` | string | `none`, `high_school`, `associate`, `bachelors`, `masters`, `phd`, or `unknown`. Unfinished coursework is not a degree. |
| `education.field` | string | Field of study, or empty. |
| `skills` | list | Tools, methods and domains the resume actually shows. Only what is written there. |
| `domains` | list | Industries and product types the user has worked in. |
| `licenses_clearances` | list | Only ones on the resume. |
| `has_managed_people` | bool | True only if the resume shows direct reports. |
| `work_authorization` | string | Ask only if it matters (for example the user is outside the US or needs sponsorship). Otherwise `""`. |
| `locations.home` | string | City and state or region the user lives in. |
| `locations.home_state` | string | State or province code, used for remote eligibility checks (for example `OR`). |
| `locations.country` | string | Country they can work in. Default `United States`. |
| `locations.places` | list | Places where the user will work in person or hybrid. Each: `name` (city, state), `aliases` (lowercase city and nearby town names that appear in job location fields), `modes` (any of `onsite`, `hybrid`, `remote`). Include relocation targets here too. |
| `locations.remote_ok` | bool | True if fully remote roles in their country are welcome. |
| `locations.relocate_to` | list | Names of places in `places` that need a move. Used in fit notes. |
| `preferences.work_modes_ranked` | list | Order of preference, e.g. `["hybrid", "remote", "onsite"]`. |
| `preferences.avoid` | list | Hiring steps or job traits they want penalized, in their words (e.g. "take-home assignments"). |
| `preferences.notes` | string | Anything else that should nudge scores, in their words. |
| `excluded_companies` | list | Employers to drop before scoring. Whole-word match, case-insensitive. |
| `filters.title_include` | list | Lowercase phrases; a posting's title must contain one. Derive from `target_titles` plus close variants and abbreviations (e.g. "product manager", "product mgr"). Keep each phrase short and specific. |
| `filters.title_exclude` | list | Lowercase words that drop a title. Use seniority words above the user's level (for a mid-level user: staff, principal, director, head of, vp, vice president, chief, lead) plus "intern" unless they want internships, plus any titles they ruled out. |
| `min_score` | number | Default `6`. |
| `posted_within_days` | number | Default `14`. |
| `max_roles_per_run` | number | Default `30`. Lower it to save usage, raise it for wider coverage. |
| `schedule.cron` | string | The cron expression given to the scheduling tool, with `CRON_TZ=<zone>` in front. |
| `schedule.label` | string | Plain words, e.g. "Weekdays around 7:40 AM". |
| `dashboard.slug` | string or null | The artifact slug of the "Job matches" page, filled once setup publishes it. |

## Boards file

A separate JSON object, one list per platform. Names are optional but make the dashboard read better.

```json
{
  "greenhouse": [{"slug": "examplecorp", "name": "Example Corp"}],
  "ashby": [{"slug": "example-labs", "name": "Example Labs"}],
  "lever": []
}
```
