# Scoring rubric

Every role is scored 1 to 10 against the user's resume and profile. The score is a sum of the lines below that apply, with no base score: start from 0, add and subtract, then apply caps, then clamp to 1..10. A role is shown on the dashboard when its final score is at least `profile.min_score` (default 6).

Two rules carry most of the accuracy:

1. **Evidence or nothing.** A line applies only when the posting states it. Quote the short phrase that triggered it. Generic boilerplate ("fast-paced", "data-driven", "cross-functional") earns nothing.
2. **Resume is the source of truth.** A skill counts as the user's only when `RESUME_TEXT` shows it. Do not credit skills the profile summary implies but the resume never mentions.

## Step 1: knockouts (check these first, stop at the first one that applies)

Each knockout must rest on an explicit line in the posting. Quote it. Missing or ambiguous text is never a knockout: pass the role on to full scoring and list the gap under `unconfirmed`.

| Knockout | Score |
|---|---|
| The user's state or country is excluded ("not eligible to be hired in CA, OR, WA", "must reside in Texas", "US only" for a non-US user). Postings often hide this near the bottom under a Location heading, so read to the end. | 1 |
| Requires living in or near an area that is not in `locations.places` and offers no relocation (applies to remote roles too). | 1 |
| Onsite or hybrid in a place that is not in `locations.places`, or a place whose allowed `modes` do not include that work style. | 1 |
| Closed, expired, or posted before the window (`posted_within_days`). | 1 |
| Requires a security clearance, license or certification the resume does not show. | 2 |
| Requires work authorization or sponsorship terms the user does not meet (from `work_authorization`). | 2 |
| Title or stated duties are clearly a different job from every `target_titles` entry (for example a sales or engineering IC role that slipped through the title filter). | 1 |

## Step 2: points

Bonuses:

| Points | Line |
|---|---|
| +3 | **Core fit.** The job's main function matches one of `target_titles` (judge the duties, not only the title). |
| +1 | **Adjacent fit** instead of core fit: related function the resume clearly supports. |
| +1 each, up to +4 | **Named skills the resume shows.** Each tool, method or domain the posting names as required or central that `RESUME_TEXT` also shows. Name each one, e.g. `+1 SQL`, `+1 A/B testing`. |
| +1 | **Domain match.** The company's industry or product type matches a domain in the resume. |
| +1 | **Experience level fits.** The years asked for fall within the user's range (`years_in_target_role` for the role itself, `years_experience_total` for general experience), or the posting welcomes the user's level. |
| +2 | **Preferred work setup.** Location and work style match the user's top choice in `preferences.work_modes_ranked` for that place. |
| +1 | **Acceptable work setup.** Allowed by the profile but not the top choice. |

Penalties:

| Points | Line |
|---|---|
| -1 each, up to -4 | **Resume gap.** A requirement listed under a required or minimum heading ("Requirements", "What you'll need", "Must have", "Minimum qualifications") that the resume does not show. Preferred or nice-to-have items do not count. Name each gap. |
| -3 | **Too senior.** The title or duties sit clearly above the user's level (staff, principal, director, head of, VP), or the role manages people when `has_managed_people` is false. |
| -2 | **Process the user avoids.** The posting names a hiring step listed in `preferences.avoid` (for example a take-home assignment or a recorded video interview). Also add a flag. |
| -1 | **Other preference conflict** stated in `preferences.notes` (for example an industry the user wants to avoid). |

## Step 3: caps (applied after the sum)

| Cap | When |
|---|---|
| 5 | **Years requirement.** The posting requires more years in the target role than the user has plus 2 (for example 5+ years of product management for someone with 2). Read the heading the years line sits under: a line under "Preferred" or "Nice to have" is not a requirement, and "X years in product OR a related field" counts related experience. Say which case applied. |
| 5 | **Hard requirement missing.** A degree the user does not have with no "or equivalent experience" alternative, or another must-have the resume plainly lacks. "Or equivalent experience" passes. |
| 7 | **Core domain gap.** What the job is mostly about (its central product, domain or tool) is absent from the resume, even if the arithmetic says more. Start `fit_note` with "Gap: <what is missing>". |

Clamp the result to 1..10 after caps.

## Output, one JSON object per role

```json
{
  "key": "<copy from the candidate>",
  "company": "Acme",
  "title": "Product Manager, Payments",
  "url": "https://...",
  "location": "New York, NY",
  "workplace": "Hybrid",
  "posted": "2026-01-14",
  "salary": "$140k-$170k",
  "source": "greenhouse",
  "score": 7,
  "breakdown": [
    {"points": 3, "reason": "core fit: PM for a payments product", "quote": "own the roadmap for our payments platform"},
    {"points": 1, "reason": "SQL", "quote": "comfortable writing SQL"},
    {"points": -1, "reason": "gap: Kafka", "quote": "experience with Kafka required"},
    {"points": "cap 7", "reason": "core domain gap: payments", "quote": ""}
  ],
  "flags": ["Process: take-home assignment"],
  "fit_note": "One or two sentences: where this resume competes for this job, and the biggest gap.",
  "resume_angle": "Which resume experience to lead with when applying.",
  "unconfirmed": ["degree wording", "travel"],
  "confidence": "high",
  "company_note": "Optional: one sentence on what the company does, stage, and any recent layoffs, or empty."
}
```

- `points` is a number for bonuses and penalties, or a string like `"cap 5"` or `"knockout"` for caps and knockouts.
- `confidence` is `high` when every line above was settled from the posting text, `medium` when one material fact was missing, `low` when the posting could not be read in full.
- For a knockout, return the same object with `score` set from the knockout table, one breakdown entry with `"points": "knockout"`, and the quote.
- A role whose posting could not be read at all gets `"score": null` and `"unconfirmed": ["posting could not be read: <reason>"]`. It is remembered but never shown as a scored card.

## Calibration notes

- When in doubt between two scores, choose the lower one.
- Do not double count: a missing degree that already triggers the cap 5 does not also take -1 as a resume gap.
- A score of 8 or more should be rare and mean "apply this week". If more than a third of a day's roles score 8+, the bonuses are being applied too loosely: re-check that each one has a quote.
