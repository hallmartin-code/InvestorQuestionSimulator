# TEN Capital Fundraising Interview Simulator

A practice venture-fundraising interview, grounded in the founder's own pitch deck. The
application reads a PDF or PowerPoint deck, builds a company profile with per-field provenance,
finds evidence gaps, and then plays a demanding investor: one question at a time, adapting to
every answer, scoring and coaching each response, and finishing with a readiness score, a
practice plan and a one-page PDF report. With consent, it returns a structured analytics
record to TEN Capital.

---

## 1. What the application does

| Step | What happens |
|---|---|
| Upload | Founder uploads a `.pdf`, `.pptx` or `.ppt` deck, optionally adds company details and notes, and sets consent. |
| Company review | Extracted fields are shown with source slide, evidence quote, status and confidence. The founder corrects or fills anything. |
| Interview | A tailored investor question appears. After each answer: four-dimension score, investor assessment, what worked, what to improve, a better answer structure, a model answer, and the next question (a follow-up when the answer left an issue unresolved). |
| Results | Overall readiness score (0–100), classification, category scores, strongest/weakest areas, highest-risk topic, most and least credible arguments, practice plan, PDF download and session data download. |

The simulator coaches communication; it never manufactures evidence. Model answers use
bracketed placeholders such as `[ARR]` or `[customer count]` wherever the founder's material
does not provide a figure, and any figure the model writes that cannot be traced to the deck,
the founder's inputs or answers is replaced automatically.

---

## 2. Architecture

```
Deck Upload → File Validation → Text/Table Extraction → Company Profile Creation
  → Evidence Mapping → Interview Question Planning → Interactive Investor Interview
  → Answer Scoring → Coaching → Adaptive Follow-up Questions → Readiness Scoring
  → Practice Plan → One-Page PDF Generation → TEN Capital Analytics Record
```

```
app/
  main.py                 FastAPI factory, session cookie + security-header middleware
  config.py               Environment settings, startup validation
  pipeline.py             InterviewService: orchestrates every pipeline step for a session
  session.py              In-memory session state and store (hard retention)
  errors.py               User-facing exception hierarchy
  ingestion/
    file_validator.py     Size, extension, MIME, magic bytes, PPTX archive checks
    deck_loader.py        Private parse workspace, dispatch, quality warnings
    pdf_parser.py         PyMuPDF text + tables per page; page rasterization for scanned decks
    ppt_parser.py         python-pptx text, tables, charts, notes; LibreOffice for legacy .ppt
    models.py             DeckDocument / DeckPage
  analysis/
    rule_extractor.py     Deterministic keyword+figure extraction (always runs)
    company_profile.py    Model extraction with quote grounding, founder input, corrections, calculations
    evidence_mapper.py    Topic ↔ field mapping, provenance digests, evidence map
    gap_analyzer.py       Rule findings + grounded model findings (STRENGTH … RED_FLAG)
  interview/
    question_engine.py    Topic planning, difficulty, model/template questions, de-duplication
    adaptive_engine.py    Follow-up vs next-topic decisions, follow-up limits, issue resolution
    scoring_engine.py     Rubric, model evaluation call, clamping, heuristic scorer
    coaching_engine.py    Coaching assembly, figure guard, template model answers
    readiness_engine.py   Weighted readiness, classification, practice plan, final narrative
  reports/
    report_data.py        Everything the PDF shows, formatted in Python
    charts.py             Horizontal category-score chart (reportlab primitives)
    pdf_generator.py      One-page layout with a deterministic fit ladder, TEN footer
    theme.py              Palette, Open Sans registration
    template.py           Loads/validates the report template; writes REPORT_TEMPLATE.md and the blank PDF
    templates/            readiness_report.template.json (source of truth), REPORT_TEMPLATE.md, blank template PDF
  integrations/
    llm_client.py         The only Anthropic API caller (structured output, streaming, fallbacks)
    ten_analytics.py      Consent-filtered analytics record + fail-safe delivery
  models/                 Pydantic models: company, analysis, interview, scoring, analytics
  web/                    Routes, Jinja templates, CSS/JS (no inline scripts or styles)
  utils/                  Logging, security primitives, money parsing, text helpers
tests/                    pytest suite and synthetic deck fixtures
```

**Division of labour.** The model (Claude Opus 5, `claude-opus-5`) is used for interpreting deck
content, identifying evidence gaps, writing tailored questions, judging answer quality,
coaching, writing model answers and the report narrative. Python does file processing, session
management, every numeric calculation (totals, percentages, weights, classifications,
calculated fields), validation, analytics structures, the chart and the PDF.

Every model call is one schema-validated request (Pydantic structured output) with adaptive
thinking, streamed. The charter plus the session's deck text form a stable system prefix with a
cache breakpoint, so each interview turn reads the deck from the prompt cache. Server-side
refusal fallbacks (`fallbacks: "default"`) are enabled; set `LLM_USE_FALLBACKS=false` if your
account does not support that beta (the client also retries once without it if the API rejects it).

**Graceful degradation.** Nothing in the model client raises. If the model is not configured, the
founder did not consent to AI processing, or a call fails, the step falls back: rule-based
extraction, rule-based findings, profile-aware template questions, heuristic scoring, template
coaching and a deterministic narrative. The UI shows a notice when this happens.

---

## 3. Installation

Requires Python 3.12+ (developed on 3.14, deployed on 3.12).

```bash
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
cp .env.example .env            # then fill in values
```

Optional: install LibreOffice to accept legacy `.ppt` files (`soffice` must be on `PATH`).

---

## 4. Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `APP_ENV` | `development` | `production` enforces required secrets, Secure cookies, HSTS and https analytics. |
| `ANTHROPIC_API_KEY` | — | Claude API key. Required in production unless `LLM_ENABLED=false`. |
| `IC_APP_TOKEN` | — | Shared access token for the app (16+ chars). Required in production. |
| `TEN_ANALYTICS_ENDPOINT` | — | HTTPS endpoint receiving analytics records (POST JSON). |
| `TEN_ANALYTICS_API_KEY` | — | Bearer token for the endpoint. Required when the endpoint is set. |
| `MAX_UPLOAD_MB` | `25` | Upload size limit (1–100). |
| `DATA_RETENTION_HOURS` | `24` | Hard lifetime of a session and everything in it. |
| `UPLOAD_DIR` | system temp `/ten-iqs-uploads` | Owner-only directory where decks exist only while parsing. |
| `LLM_ENABLED` | `true` | `false` forces heuristic mode. |
| `LLM_MODEL` | `claude-opus-5` | Model ID. |
| `LLM_EFFORT` / `LLM_QUESTION_EFFORT` | `high` / `medium` | Effort for extraction, gaps, scoring, summary / for question writing. |
| `LLM_TIMEOUT_SECONDS`, `LLM_MAX_RETRIES` | `240`, `2` | Client timeout and SDK retries. |
| `LLM_USE_FALLBACKS` | `true` | Server-side refusal fallbacks (beta). |
| `INTERVIEW_MAX_QUESTIONS` | `12` | Answers per interview, follow-ups included (3–30). |
| `INTERVIEW_MAX_FOLLOWUPS_PER_TOPIC` | `2` | Follow-up cap per topic (0–4). |
| `SESSION_COOKIE_SECURE` | production: `true` | Override the Secure cookie flag. |
| `ANALYTICS_TIMEOUT_SECONDS` | `10` | Per-attempt analytics timeout. |
| `LOG_LEVEL` | `INFO` | Log level. |

Configuration is validated at startup (`Settings.validate_startup`): unusable settings stop the
app with a list of problems; degraded modes are logged as warnings.

---

## 5. Running locally

```bash
uvicorn app.main:create_app --factory --reload
# or
python -m app.main
```

Open http://127.0.0.1:8000. Without `ANTHROPIC_API_KEY` the app runs in heuristic mode, which is
useful for UI work. To try the full experience, set the key and tick "Use AI to run the
interview" on the upload screen. Sample decks: `python tests/fixtures/build_fixtures.py` writes
synthetic decks into `tests/fixtures/`.

---

## 6. Supported file types

| Type | Handling |
|---|---|
| `.pdf` | PyMuPDF: text per page, table rows, page numbers preserved. Password-protected and corrupt files are rejected with instructions. Image-only (scanned) decks are detected; with AI consent, up to 15 page images are read by the model and those values are marked low-confidence and "please confirm". |
| `.pptx` | python-pptx: text frames, grouped shapes, tables, chart titles and series, speaker notes, one page per slide. |
| `.ppt` | Converted to `.pptx` with LibreOffice when installed; otherwise the user is told how to convert it. |

A file is accepted only when its extension, declared MIME type and magic bytes agree. PPTX
archives must contain `ppt/presentation.xml` and are checked for zip-bomb characteristics.

---

## 7. Interview methodology

**Profile and provenance.** Each field records status (`VERIFIED_FROM_DECK`, `USER_PROVIDED`,
`CALCULATED`, `INFERRED`, `MISSING`), claim type (`FACT`, `FOUNDER_CLAIM`, `ASSUMPTION`,
`CALCULATION`, `INFERENCE`, `MISSING`), slide, verbatim evidence and confidence. A deck figure is a
founder claim unless it carries a third-party citation; plans and projections are assumptions.
Model-extracted values are kept only if the cited slide's text contains the quote and the
value's figures; "inferred" values are allowed only for sector, subsector, stage, business model
and target customer. Projections are never recorded as achieved revenue.

**Gap analysis.** Rules flag missing information expected at the company's stage, unsourced or
inconsistent market sizing, commitments above the round size, conflicting figures across
slides, short runway, implausible LTV:CAC, high revenue multiples, "no competition" claims and
unsupported superlatives. The model adds judgment findings; any quote it cites is verified and
unverifiable red flags or inconsistencies are downgraded to questions.

**Planning.** Each of the 19 topics gets a priority: a stage-specific base weight plus the weight
of its open findings (RED_FLAG 3, INCONSISTENCY 2.5, EVIDENCE_GAP 2, QUESTION 1, STRENGTH 0.4,
scaled by severity) plus a missing-information bonus. About three quarters of the question budget
goes to the highest-priority topics (fundraising is always included); the remainder is reserved
for follow-ups. Topics run in meeting order: understanding → evidence → investment committee.

**Difficulty.** Level 1 (understanding) for the first third of the interview, Level 2 (evidence)
for the middle third, Level 3 (investment committee) for the final third. Follow-ups are at least
one level above the question they follow.

**Questions.** The model writes each question from the topic's profile entries, open findings,
the transcript and the list of questions already asked. Near-duplicates (token Jaccard ≥ 0.6 or
sequence similarity ≥ 0.85) are retried once, then replaced by a profile-aware template question.
Only one question is ever active; the plan is never shown.

**Adaptive follow-ups.** After each answer, a follow-up is asked when evidence, credibility or
completeness is ≤ 2/5, the evaluator found a contradiction, or it flagged an unresolved issue on
an answer below 16/20. Limits: `INTERVIEW_MAX_FOLLOWUPS_PER_TOPIC` per topic; the interview moves
on if a founder still scores ≤ 6/20 after a follow-up (the gap goes into the practice plan); the
last question slot is never spent on a follow-up while planned topics remain. An answer scoring
14/20 or more resolves the findings its question was probing.

---

## 8. Scoring methodology

**Per answer.** Clarity, Evidence, Credibility and Completeness, each 0–5 against a fixed rubric
(the model proposes them; Python clamps to 0–5).
`response_score = clarity + evidence + credibility + completeness` (max 20);
`response_percentage = response_score / 20 × 100`. In heuristic mode the four dimensions come
from directness, figures and evidence terms, hedging and absolute claims, and coverage of the
question's terms.

**Readiness (0–100).** A category's score is the mean response percentage of the answers on its
topics. The overall score is the weighted mean over the categories the interview covered:

| Category | Weight | Topics |
|---|---|---|
| Market Understanding | 10 | Market |
| Problem & Customer | 10 | Problem, Customer |
| Product & Differentiation | 10 | Product/Solution, Defensibility |
| Traction | 15 | Traction |
| Business Model & Economics | 15 | Business Model, Unit Economics |
| Go-to-Market | 10 | Go-to-Market |
| Competitive Position | 5 | Competition |
| Team | 10 | Team |
| Financial Command | 5 | Financial Performance, Financial Projections |
| Fundraising Strategy | 5 | Fundraising, Valuation, Use of Funds, Milestones, Exit Potential |
| Risk Awareness | 5 | Risks |

Uncovered categories are excluded (shown as n/a) rather than scored as zero; coverage (the share
of weight assessed) is reported, and the score is marked **provisional** when coverage is below
60% or fewer than five questions were answered.

| Score | Classification |
|---|---|
| 90–100 | INVESTOR READY |
| 80–89 | STRONG — MINOR PREPARATION NEEDED |
| 70–79 | PROMISING — SEVERAL WEAK AREAS |
| 60–69 | SIGNIFICANT PREPARATION NEEDED |
| < 60 | NOT YET READY FOR INVESTOR OUTREACH |

Also reported: strongest and weakest category; highest-risk topic (100 − topic score, plus
12/8/5/2 × severity for each open red flag, inconsistency, evidence gap and question); most
credible and weakest argument (highest and lowest evidence + credibility).

**Practice plan.** The 3–5 weakest categories (below 80, weakest first). Each has the weakness,
why investors care, missing evidence (fields absent from the deck plus thin answers), a
recommended action and a practice question: the actual question the founder struggled with when
it scored under 60%, otherwise a standard one.

---

## 9. PDF generation

**Report template.** The report's structure lives in one file,
[`app/reports/templates/readiness_report.template.json`](app/reports/templates/readiness_report.template.json),
which holds no company data. It defines:
- page geometry, type scale, palette, markers and the fit ladder;
- section order, headings and placement;
- every field: label, type, source binding, format, length limit, placeholder, empty text and rule;
- the fundraising-snapshot rows and their profile bindings;
- the category-score rows, which validation requires to match the scoring weights;
- the footer, the controlled vocabularies and the analysis rules.

`report_data.py` and `pdf_generator.py` read their headings, labels, limits and fixed text from it at runtime,
and an invalid template fails with a `ConfigError` that says what is wrong. After editing the JSON, run
`python -m app.reports.template` to regenerate
[`REPORT_TEMPLATE.md`](app/reports/templates/REPORT_TEMPLATE.md) (the human-readable specification) and
`readiness_report_template.pdf` (the blank page drawn with placeholders through the real renderer). A test fails
if the committed Markdown is stale.

`reports/pdf_generator.py` renders a US Letter page with reportlab: title, company/sector/stage/
raise line, overall score and classification, investor assessment, category-score bar chart
(60/80 guides), fundraising snapshot, strongest and weakest areas, investor concerns, questions
to practice and next steps. Title 17 pt, section headers 9.5 pt uppercase, body 8.5 pt.

The layout fits deterministically: it renders at up to four progressively tighter settings (font
size, item counts, text length) and returns the first one-page result. Calculated and inferred
snapshot values are labelled as such. The footer carries "Confidential – for recipients only" and
the TEN Capital footer line (document title, page number, "Compiled on [date] by TEN Capital
Network", logo) in Open Sans 7 pt. Fonts fall back to Helvetica if the bundled Open Sans files are
missing. A rendering failure raises `ReportError`; the on-screen results remain available.

---

## 10. TEN Capital analytics integration

When the interview is finished, `integrations/ten_analytics.py` builds a record in the documented
schema (`session_id`, `timestamp`, `user`, `company`, `raise`, `interview`, `engagement`, `consent`)
and POSTs it as JSON with `Authorization: Bearer $TEN_ANALYTICS_API_KEY`.

| Consent granted | Record contents |
|---|---|
| none | Nothing is built or sent. |
| analytics | Sector, subsector, stage; questions asked, average answer score, readiness score and level, strong/weak/highest-risk topics; completion and duration. |
| + data storage | Adds company name, geography, raise amount, committed, valuation, instrument, runway. |
| + marketing | Adds user name, email and a pseudonymous user id (SHA-256 of the email). |

Deck text and founder answers are never included. `session_id` is a random analytics id,
unrelated to the session cookie. Delivery uses one retry for network errors and 5xx responses,
none for 4xx. Failures are recorded as `failed` with a reason and shown on the results page;
they never block the assessment.

### Results email (Resend)

When an interview finishes, `integrations/email_notifier.py` emails the results to the TEN Capital inbox through
the Resend API (`POST https://api.resend.com/emails`).

**Who receives it.** Everyone in `EMAIL_TO` (default `Info@tencapital.group`). Recipients come only from
configuration, never from anything a user enters.

**What it contains**
- **Subject:** `[Interview Readiness] <Company> — <score>/100 <classification>`, plus "(provisional)" when applicable.
- **Summary:** company, sector, stage and raise; the score with its coverage facts; the investor assessment;
  category scores; strongest and weakest areas; concerns; the practice plan; next steps; the fundraising
  snapshot; each question with its score; open deck findings; and the consent flags.
- **Attachment:** the one-page PDF.

**What needs founder consent**

| Content | Included when |
|---|---|
| Summary and one-page PDF | Every finished interview (the upload page tells founders) |
| Session JSON with the founder's answers, evidence map and findings | Founder granted data-storage consent |
| Founder name and email | Founder granted follow-up (marketing) consent |

Set `EMAIL_REQUIRES_STORAGE_CONSENT=true` to email only sessions where the founder granted data-storage consent.

**Delivery**
- `EMAIL_FROM` must be on a domain verified in Resend; `tencapital.group` is verified.
- A send is retried once after network errors, 429 or 5xx responses. An idempotency key per session prevents
  duplicate emails.
- A failure is shown on the results page and logged by error class. It never blocks the founder's results.

| Variable | Default | Purpose |
|---|---|---|
| `RESEND_API_KEY` | — | Resend API key; emails are off without it. |
| `EMAIL_ENABLED` | `true` | Master switch. |
| `EMAIL_TO` | `Info@tencapital.group` | Comma-separated recipients. |
| `EMAIL_FROM` | `TEN Capital Interview Simulator <reports@tencapital.group>` | Sender on a verified domain. |
| `EMAIL_REPLY_TO` | — | Optional reply-to address. |
| `EMAIL_ATTACH_PDF` / `EMAIL_ATTACH_JSON` | `true` / `true` | Attachments (JSON still requires data-storage consent). |
| `EMAIL_REQUIRES_STORAGE_CONSENT` | `false` | Email only consented sessions. |
| `EMAIL_TIMEOUT_SECONDS` | `15` | Per-attempt timeout. |

---

## 11. Security considerations

- **Access.** `IC_APP_TOKEN` gates the app (constant-time comparison, per-client failed-login
  throttling, session id rotated on login).
- **Sessions.** Random 256-bit id in an `HttpOnly`, `SameSite=Lax` cookie (`Secure` in production).
  State is server-side only and deleted after `DATA_RETENTION_HOURS` regardless of activity; the
  store evicts least-recently-used sessions beyond 2,000.
- **CSRF.** Every state-changing request is a POST with a per-session token.
- **Uploads.** Size limit enforced from `Content-Length` before parsing and again on the bytes;
  extension, MIME and magic-byte agreement; PPTX structure and decompressed-size checks; filenames
  sanitized for display only. Bytes are written under a fixed name into a random owner-only
  directory inside `UPLOAD_DIR`, parsed and deleted before the request returns; orphans older than
  an hour are purged. Uploaded files are never served back; the PDF and JSON downloads are bound to
  the session cookie, not to guessable URLs.
- **Headers.** Strict CSP (no inline script or style), `X-Frame-Options: DENY`, `nosniff`,
  `Referrer-Policy: no-referrer`, `Cache-Control: no-store`, HSTS in production. API docs are disabled.
- **Secrets.** Environment variables only, held as `SecretStr`; `.env` is git-ignored.
- **Model safety.** Deck and answers are delimited and declared data, not instructions; extracted
  facts are grounded against slide text; coaching figures are guarded against the source.
- **Logging.** Counts, stages and error classes only — no deck text, answers, names or emails.

---

## 12. Testing

```bash
pytest                      # full suite
pytest --cov=app            # with coverage
```

The suite generates synthetic decks (a 12-slide PDF, a 13-slide PPTX with a table and speaker
notes, a scanned PDF, a sparse deck, corrupted PDF/PPTX and a Word archive disguised as PPTX) and
uses a fake model client, so it runs offline with no API key. It covers PDF and PowerPoint
ingestion, unsupported and corrupted files, missing data, extraction grounding, gap analysis,
scoring and readiness arithmetic, classification thresholds, question de-duplication, adaptive
follow-up logic, the figure guard, analytics consent enforcement and failure handling, PDF
generation (one page, required sections), incomplete interviews, model outages, configuration
validation, the model client's request shape and error handling, and the full browser flow
including CSRF, the access-token gate and security headers.

---

## 13. Deployment

**Docker**

```bash
docker build -t ten-interview-simulator .                       # add --build-arg INSTALL_LIBREOFFICE=true for .ppt
docker run -p 8000:8000 --env-file .env ten-interview-simulator
```

**Railway (current deployment).** The service `investor-question-simulator` in the Railway project of the
same name builds from the `Dockerfile` (`railway.json` sets the builder, the `/health` health check and one
replica). `.railwayignore` keeps `.env` and tests out of the upload; secrets exist only as Railway variables.

1. Check the API key locally first. The check calls the Models API and spends no tokens:

   ```bash
   python -m app.integrations.llm_client     # environment variables take precedence over .env
   ```

2. Set the service variables. Pipe secrets through stdin so they never appear in shell history:

   ```bash
   railway variable set ANTHROPIC_API_KEY --stdin --service investor-question-simulator
   railway variable set IC_APP_TOKEN --stdin --service investor-question-simulator
   railway variable set APP_ENV=production LLM_MODEL=claude-opus-5 --service investor-question-simulator
   ```

   Optional analytics: `TEN_ANALYTICS_ENDPOINT` and `TEN_ANALYTICS_API_KEY`.

3. Deploy and get the URL:

   ```bash
   railway up --ci --service investor-question-simulator
   railway domain list --service investor-question-simulator
   ```

To rotate the Claude key: create a new key in the Claude Console, repeat the `ANTHROPIC_API_KEY` command in step 2
(Railway redeploys automatically), then revoke the old key. Set a monthly spend limit in the Console as well,
because every interview turn calls Claude Opus 5.

**Other Heroku-style platforms** use the `Procfile`; set the same environment variables and `APP_ENV=production`.

Notes:

- Run **one worker process**. Sessions live in process memory; multiple workers or replicas would
  not share them. To scale out, replace `SessionStore` with a shared store (e.g. Redis with the same
  retention TTL) and use sticky sessions or shared state.
- Model calls can take tens of seconds per step. Keep proxy/request timeouts at 5 minutes or more.
- Terminate TLS in front of the app; uvicorn runs with `--proxy-headers`.
- `/health` returns `{"status": "ok"}` for health checks.
