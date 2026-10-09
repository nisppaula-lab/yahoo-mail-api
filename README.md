# Yahoo Mail API for Muse

A tiny standard-library Python API that exposes Yahoo Mail over IMAP/SMTP to Muse through a Bearer-token REST API.

Why this shape: Yahoo Mail does not provide a simple consumer OAuth mail API for this use. The Yahoo app password stays in the external host's environment; Muse only receives the API base URL and a Bearer token through its secure connector flow.

See `API.md` for endpoints and setup.
