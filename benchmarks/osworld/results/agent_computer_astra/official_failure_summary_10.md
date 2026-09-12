# First 10 official task failures

This table lists the first ten task IDs with at least one `FAILURE` verdict from the official OSWorld evaluator. Infrastructure events (including `RATE_LIMITED`) are not counted as failures. One task (`3ef2b351-...`) is a mixed outcome: one replica passed and two failed.

| Task ID | Task objective | Why it failed |
|---|---|---|
| `3ef2b351-8a84-4ff2-8724-d86eae9b842e` | Center-align the heading (the first line) in the supplied LibreOffice Writer document. | The official `is_first_line_centered` check returned `0.00` in two of three scored replicas, so the heading was not reliably centered. One replica did pass. |
| `550ce7e7-747b-495f-b122-acdc4d0b8e54` | Apply strikethrough to the first two lines of the soccer-club to-do list in the Impress presentation. | The agent returned `FAIL` in all scored replicas; the official PPTX comparison returned `0.00`, meaning the saved presentation did not match either accepted reference. |
| `8ba5ae7a-5ae5-4eab-9fcc-5dd4fe3abf89` | Set VLC's recordings output folder to the Desktop. | The agent returned `FAIL` in the scored attempts and the official VLC configuration check returned `0.00`; the recording path was not set to `/home/user/Desktop`. |
| `937087b6-f668-4ba6-9110-60682ee33441` | Make VLC the default video player on Ubuntu. | The agent returned `FAIL`; the official include/exclude check returned `0.00`, so `vlc.desktop` was not present in the default-video-player configuration. |
| `982d12a5-beab-424f-8d38-d2a48429e511` | Change the VS Code color theme to **Visual Studio Dark**. | Although the agent reported `DONE`, all three official configuration comparisons returned `0.00`; `settings.json` did not contain the required theme setting. |
| `acb0f96b-e27c-44d8-b55f-7cb76609dfcd` | Clone `xlang-ai/instructor-embedding` into `/home/user`. | The agent returned `FAIL` and the official directory-listing comparison returned `0.00`; the expected repository content was not present. |
| `d52d6308-ec58-42b7-a2c9-de80e4837b2b` | Hide the left dock in GIMP. | The agent reported `DONE`, but all official GIMP configuration checks returned `0.00`; `hide-docks` was not persisted as `yes` in `sessionrc`. |
| `e0df059f-28a6-4169-924f-b9623e7184cc` | Rename the Desktop folder `todo_list_Jan_1` to `todo_list_Jan_2`. | The agent returned `FAIL`; the official exact-match check returned `0.00`, so the target directory did not exist at the required path. |
| `e2b5e914-ffe1-44d2-8e92-58f8c5d92bb2` | Disable VS Code's Python `reportMissingImports` diagnostic. | The only scored replica reported `DONE`, but the official JSON settings check returned `0.00`; the required diagnostic-severity override was absent or different. The other attempts were rate-limited and are not counted. |
| `f9be0997-4b7c-45c5-b05c-4612b44a6118` | Turn on Ubuntu's Do Not Disturb mode. | The agent returned `FAIL` in all scored replicas and the official exact-match check returned `0.00`; `show-banners` was not set to `false`. |
