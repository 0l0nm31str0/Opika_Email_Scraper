Launch the Opika Leads web scraper UI and start a scraping session.

Follow these steps EXACTLY:

1. **Install dependencies** (only if not already installed):
   ```
   cd /home/user/Opika_Email_Scraper && pip install -r requirements.txt --quiet 2>/dev/null
   ```

2. **Kill any existing Opika server** on port 5000:
   ```
   lsof -ti:5000 | xargs kill -9 2>/dev/null || true
   ```

3. **Start the Flask server** in the background:
   ```
   cd /home/user/Opika_Email_Scraper && python app.py &
   ```
   Wait 2 seconds for the server to boot.

4. **Open the browser** to the UI:
   ```
   python -m webbrowser "http://localhost:5000"
   ```

5. **Report to the user**:
   Tell the user:
   - The Opika Leads UI is now running at **http://localhost:5000**
   - They can fill in the form (industry, location, titles) and click "Start" to begin scraping
   - Logs stream live in the browser via SSE
   - When complete, they can download the CSV/JSON results directly from the UI
   - To stop the server later, run: `lsof -ti:5000 | xargs kill -9`

If the user provided arguments like `$ARGUMENTS`, parse them as:
- First argument = industry (e.g., "SaaS", "plumbing", "dentist")
- Second argument = location (e.g., "San Francisco", "NYC")

If arguments were provided, also tell the user they can pre-fill those values in the UI form.
