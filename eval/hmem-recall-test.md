# H-MEM in the loop: does a past incident change the next investigation?

Run on 2026-10-24 against the URL shortener (real Docker Compose app), with the memory service holding 12 memories from earlier incidents.

**Setup.** `docker kill shortener_db_postgres`, with the Overwatch console watching.

**What the Historian did.** It recalled two past incidents from H-MEM: `mem-10` (a past `postgres_down`, relevance 0.31) and `mem-8` (a past `redis_down`, relevance 0.29). Both scored below the engine's default cut-off of 0.35, so the earlier code *dropped* them. Two changes fixed it: the Historian asks for a threshold of 0.25, and it searches on the symptoms alone (raw check names such as `event:postgres_down`, plus what each check said). Evidence text from the first seconds of an outage is left out: in testing it only added noise and pushed a near-identical incident under the threshold. With symptoms alone, a later run recalled three past incidents at relevance 0.49 to 0.52 and the investigator used all three, finishing in one step.

**What the Investigator did.** It marked `mem-10` as used in its diagnosis (`similar_incidents[].used = true`) and left `mem-8` unused. The diagnosis cited the earlier kills at 19:02, 19:09 and 19:16 as a pattern.

**What happened after approval** (H-MEM ledger):

| memory | action | detail |
|---|---|---|
| mem-10 | read | recalled for this incident |
| mem-8 | read | recalled for this incident |
| mem-10 | feedback | `helpful`, confidence +0.05 (it was used) |
| mem-13 | create | this incident stored for next time |

The memory count went 12 → 13. So the loop is closed: recall, use, credit the memory that helped, store the new incident.

Reproduce: `docker kill <db container>` on a system that has had a similar incident approved before, open the incident, and look at *How Overwatch figured it out → Historian*.
