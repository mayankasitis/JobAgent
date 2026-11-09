# JobAgent

A small, human-in-the-loop job application assistant. It finds jobs on company career pages,
scores them against your profile with Claude, drafts a cover letter, and **fills the application
form in a real browser**. You review the highlighted fields and click **Submit** yourself.

```
discover (ATS APIs) → keyword prefilter → Claude score → queue
apply → resume + cover letter → browser opens → fields extracted → Claude maps → filled
      → 🟩 filled  🟧 review  🟥 missing → you click Submit → mark done
```

## Features

- **Discovery without scraping:** it reads the public job feeds of Greenhouse, Lever, Ashby,
  SmartRecruiters and Workday, the same JSON the companies' career pages load.
- **One generic form filler instead of a script per site:** it reads every field, including iframes,
  React-style comboboxes, Workday listboxes and hidden custom radio buttons. Claude maps the fields
  to your profile and Playwright fills them. It also works on Darwinbox, Keka, Zoho Recruit, LinkedIn
  Easy Apply and others through `add-url`.
- **Never invents facts:** your resume text is the source of truth. Unknown answers are left
  blank and highlighted. Wrong values that the ATS pre-filled from your resume are corrected and flagged.
- **Learns:** press `l` to save the answers you typed by hand, so they get reused next time.
- **Never auto-submits,** and has **daily caps** per source.
- **Local SQLite tracker,** CSV export, and a macOS `launchd` daily schedule.

## Quick start (macOS / Linux, Python 3.10+)

```bash
git clone <this repo> && cd jobagent
./scripts/setup.sh          # venv, deps, Chromium, creates config/ from config.example/, asks for API key
```

Then edit your (git-ignored) `config/`:

| file | what |
|---|---|
| `profile.yaml` | contact details, experience, salary, education, your own guardrails for the AI |
| `answers.yaml` | reusable screening answers |
| `resume.txt` | plain text of your resume (source of truth) — PDF goes in `resumes/` |
| `criteria.yaml` | titles, locations, keywords, score threshold, daily caps |
| `companies.yaml` | target companies + their ATS (`detect-ats <url>` helps) |

```bash
python -m jobagent doctor            # what's still missing
python -m jobagent verify-companies --disable-failed
python -m jobagent daily             # discover + score
python -m jobagent list -v
python -m jobagent apply --next      # browser opens; review, Submit, then press d
```

Keys inside `apply`: **Enter** fills the current page (press it again after each *Next* in
multi-step forms), `a` clicks Apply, `o` re-fills everything, `l` learns your edits,
`d` marks it done, `s` skips, `q` stops.

Other commands: `add-url`, `refilter`, `show`, `mark`, `stats`, `export`. Run
`./scripts/install_schedule.sh` to discover and score every morning.

## Privacy

Everything personal (`.env`, `config/`, `resumes/`, `data/`) is git-ignored. Sign-ins
happen in a separate browser profile (`~/.jobagent/browser-profile`), and passwords are
never stored or sent to the model. The job description, your profile and your resume
text are sent to the Anthropic API for scoring and form mapping.

## Tests

```bash
python -m pytest -q tests     # offline: fixture forms + parsers, no API key needed
```

## Notes

Respect each site's terms. Many job boards prohibit bulk automation. This tool is built
for reviewed, low-volume applications to company career pages.

MIT licensed.
