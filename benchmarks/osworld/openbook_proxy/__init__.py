"""Controlled fixture gateway for the GPT Astra open-book campaign.

Two deployment sites share the same bundle format and lookup logic (bundle.py):
  - guest-side (env/guest_proxy.py starts it inside the Daytona sandbox, port 18888, the same
    port OSWorld's own SetupController.use_proxy already points Chrome at -- see setup.py:309-310):
    serves fixtures to the agent's own Chrome and to the CDP-driven getters
    (info_from_website/pdf_from_url/gotoRecreationPage) that connect to that same Chrome.
  - host-side (env/host_proxy.py starts it on the harness host, scoped to one evaluate_official()
    call via HTTP(S)_PROXY env vars): serves get_cloud_file's direct requests.get(url), and, with
    drive_mock.py attached, the pydrive2 OAuth2/Drive API calls get_googledrive_file makes.

Neither site ever forwards a request it can't resolve against the bundle -- see addon.py's
explicit-diagnostic-response contract (docs/g_astra_open_book_runner_implementation.md,
"Snapshot service").
"""
