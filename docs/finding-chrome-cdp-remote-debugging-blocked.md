# Finding: Chrome's remote-debugging port has always been blocked, and the fix is not yet applied to the closed-book image

## The bug

`google-chrome --remote-debugging-port=1337`, launched exactly as
`SetupController._launch_setup` does it (via the `/usr/bin/google-chrome` wrapper,
`--user-data-dir=$HOME/.config/google-chrome`), logs:

```
DevTools remote debugging requires a non-default data directory. Specify this using --user-data-dir.
```

and the CDP port never binds. Confirmed live 2026-09-12 on a real Daytona sandbox (amd64
hardware, not a QEMU-emulation artifact) on **both** the currently pinned production image
(Chrome 151.0.7922.108) and a freshly rebuilt one (153.0.8010.36) — this is not new drift, it
has always been broken.

Root cause: Chromium (~M92+) refuses remote debugging whenever `--user-data-dir`'s VALUE equals
what `chrome::GetDefaultUserDataDirectory()` computes internally, even when the flag is passed
explicitly — an anti-hijacking check comparing paths, not "was the flag given." The wrapper's
value (`$HOME/.config/google-chrome`) IS the platform default, and it has to be: `desktop_env`'s
evaluator getters (`getters/chrome.py`) hardcode reading Chrome's profile from that exact path.

Chrome itself, the agent's pyautogui-driven actions, and Preferences-file-based evaluator getters
(confirmed live: `enable_do_not_track`, `is_expected_bookmarks`) are unaffected — only getters
that connect via `playwright.connect_over_cdp` need the port: `info_from_website`,
`pdf_from_url`, `gotoRecreationPage_and_get_html_content`.

## The fix (verified, applied to the open-book image only)

```dockerfile
ENV XDG_CONFIG_HOME=/opt/osw_xdg_unused
```

baked into the image root (inherited by supervisord → start.sh → main.py → every Chrome launch).
`chrome::DIR_USER_DATA`'s default computation on Linux reads `XDG_CONFIG_HOME`; shifting it makes
the "what would my default have been" comparison come out false, without changing the ACTUAL
directory Chrome uses (still whatever `--user-data-dir` says). Verified live, twice:

1. On `docker/Dockerfile.osworld-openbook`'s image (layered on the pinned closed-book digest):
   CDP responds with a real JSON envelope through both the direct port and the task's own
   `socat` forward; `$HOME/.config/google-chrome` still populates normally (`Default/`,
   `Local State`, etc.).
2. Separately, on a throwaway image that is the **production** pinned digest
   (`sha256:8917c3643b19f14d85aaa4f66ef87aa789854ae8529d6a6fe8151f0edc973f6b`) plus *only* this
   one `ENV` line, nothing else changed: identical result — CDP works, profile directory
   untouched. Confirms the fix is compatible with the actual production image, not just the
   open-book variant. (That throwaway image, `ghcr.io/longobucco/osworld-ab:verify-xdg-fix-temp`,
   is still on the registry, unreferenced by any config — safe to delete.)

Applied in `benchmarks/osworld/astra_openbook_campaign_lock.json`'s
`network_policy.daytona_image_digest` (the open-book campaign only).

## Why it is NOT applied to the closed-book image (`docker/Dockerfile.osworld`, `config.IMAGE`)

Deliberately deferred, not forgotten:

1. **Low urgency for the existing campaign.** Of the closed-book lock's 291-task frozen
   population, exactly **1** task uses a CDP-driven getter (`info_from_website`). The other 290
   are unaffected by this bug either way.
2. **Unverified risk of reproducing the same bug class elsewhere.** `XDG_CONFIG_HOME` is the XDG
   Base Directory variable several GTK/Qt apps in this image (VLC, GIMP, Thunderbird) consult to
   decide where to write their OWN config — while their evaluator getters read a **hardcoded**
   path instead (`getters/gimp.py`: `~/.config/GIMP/2.10/...`; `getters/vlc.py`:
   `~/.config/vlc/vlcrc` — `~` expanded from `$HOME`, not from `$XDG_CONFIG_HOME`). If any of
   those apps actually honor `XDG_CONFIG_HOME` at runtime, redirecting it would reproduce the
   exact divergence-between-app-state-and-evaluator-read-path bug this project already paid for
   once with Chrome's profile path (see `docker/Dockerfile.osworld`'s own comment, "found live via
   the dataset notebook's §8.3 case study") — for a different set of apps this time. Not tested.
3. **Changing `config.IMAGE`'s digest changes the closed-book campaign's identity.** This project's
   own established discipline (`config.ASTRA_SYSTEM_NAME`/`SYSTEM_NAME`'s pinning-and-naming
   pattern) is that a changed pinned configuration gets its own results tree rather than silently
   swapping under an existing name/digest, specifically so historical and new-configuration runs
   never pool together unlabeled. Applying this fix to production is not a one-line change in
   practice — it is a new campaign identity decision.

## What would need to happen before applying it to closed-book

1. Verify live whether VLC/GIMP/Thunderbird actually consult `XDG_CONFIG_HOME` for their own
   config storage, and if so, whether their evaluator getters still find the right file with it
   set to a different path.
2. Decide how the changed image is named/tracked (new digest under the same `config.IMAGE`
   env var with an explicit re-validation pass, vs. a distinct system name/results tree).
3. Rebuild, push, and re-run at least a small validation pass across the apps actually affected
   (chrome, vlc, gimp, thunderbird) before trusting it for a real campaign.
