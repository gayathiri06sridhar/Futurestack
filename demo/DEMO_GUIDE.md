# Futurestack Round 3 live demonstration

This walkthrough uses an isolated SQLite database and the fictional Everpine
brand. It does not import or modify `knowledge/brand_rules.json` or
`data/futurestack.sqlite3`. Never place real organisational data in this
demonstration database.

## Preparation

1. Install the project dependencies and copy `.env.example` to `.env` as
   described in [Readme.md](../Readme.md).
2. Set `LLM_API_KEY` in `.env` to a Gemini key for genuine AI review and live
   model evaluation. Without it, the application will explicitly fail AI
   review requests and evaluation will mark Gemini cases as skipped. Do not
   present a fabricated response as a live result.
3. Optional: set `OCR_API_KEY` to enable OCR for image submissions. Text-only
   demonstration does not require OCR.
4. Start the isolated demo environment:

   ```powershell
   .\demo\start-demo.ps1
   ```

   The first launch prompts for a temporary administrator password (12+
   characters) and creates `demo\data\futurestack-demo.sqlite3` using
   `demo\brand_rules.json`. Open [http://127.0.0.1:8000](http://127.0.0.1:8000).

## Walkthrough

1. **Set up the demo brand** — Sign in with `demo-admin@everpine.local` and
   the password entered at startup. The admin dashboard loads database-backed
   statistics.
2. **Configure objective visual checks** — In **Visual rules**, set approved
   colours to `#174d38, #ffffff`, set the logo-similarity threshold to `0.72`,
   and add a contrast pair such as:

   ```json
   [{"label":"Body copy","foreground":"#174d38","background":"#ffffff","minimum_ratio":4.5}]
   ```

   Set **Expected logo placement** to **Bottom right** and upload
   `evaluation\fixtures\approved_logo.png` as the approved logo. Upload a PDF
   or UTF-8 visual-guidelines document in **Upload visual guidelines**. The
   interface labels these image checks as objective measurements and does not
   treat Gemini's image description as a logo, placement, or colour match.
3. **Create the reviewer and creator** — Use **Create user** to add a reviewer
   account. Sign out and use **Create creator account** to register a separate
   creator. Sign in as the creator.
4. **Submit marketing copy** — Submit:

   > The Moss bottle holds 750 mL and improves focus by 300%. Our miracle
   > solution delivers guaranteed results.

   The evidence base states that Moss holds 500 mL and is made with 70%
   recycled aluminum. With `LLM_API_KEY` configured, the actual review should
   identify the unsupported or contradicted claims and cite retrieved sample
   guidance. Outcomes depend on the live model; demonstrate the returned
   evidence and findings, not a promised exact phrasing.
5. **Reviewer requests changes** — Sign out and sign in as the reviewer. Open
   the pending queue, inspect the AI findings and evidence, add a comment, and
   choose **Request changes**. The backend stores the decision, reviewer
   account, and timestamp. The AI review itself remains advisory.
6. **Creator revises and resubmits** — Sign back in as the creator, open the
   requested-changes submission, choose **Revise and resubmit**, and update it
   to:

   > Meet Moss: a practical 500 mL bottle made with 70% recycled aluminum.

   The new submission is stored as revision 2 and returns to pending human
   review. Open **Revision history** to see the original and revised content,
   reviewer comment, identity, and decision time.
7. **Reviewer approves** — Sign in as the reviewer and approve the revised
   submission. Both revisions and their decisions remain in the history.
8. **Verify persistence and reporting** — Sign in as admin. Check that the
   dashboard counts updated, load **Audit log**, and select **Verify audit
   chain**. The chain is tamper-evident only while the host administrator
   cannot alter both the SQLite file and HMAC key; it is not an immutable
   external audit service.
9. **Run and view measured evaluation** — In a second PowerShell window, from
   the project root:

   ```powershell
   python -m evaluation.run
   ```

   The runner appends a timestamped report to `evaluation\results.jsonl`.
   Return to the admin dashboard and choose **Load latest evaluation**. Live
   Gemini metrics are populated only if actual Gemini predictions were
   obtained. Visual measurements and retrieval results are separately labelled
   and use generated fixtures and synthetic ground truth.

## Sample material

- `brand_rules.json`: isolated Everpine brand guidance used only by the demo
  server process.
- `campaign_samples.json`: fictional copy examples for additional manual tests.
- `evaluation\fixtures\`: generated synthetic logo and campaign images. These
  are evaluation fixtures, not brand assets from a real organisation.

The demo SQLite file and its `.audit-key` are local runtime data. Keep them out
of real data backups and do not publish them. To reset only this demonstration,
stop the demo server and remove `demo\data\futurestack-demo.sqlite3` and
`demo\data\futurestack-demo.sqlite3.audit-key`; do not remove the production or
default application database.
