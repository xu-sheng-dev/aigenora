# Aigenora Personalization

> This file is maintained by the user or user Agent. `aigenora skill install/update` will NEVER overwrite this file.
> If this file does not exist, the Agent uses only SKILL.md defaults.

## User Preferences

<!-- Record personalized preferences here. The Agent reads and follows them. -->

### Default Parameters

<!-- Example: User always prefers best-of-3 RPS -->
<!-- host_options: {"best_of": 3, "termination": "first_to_win"} -->

### Server Configuration

<!-- Example: User overrides the default server -->
<!-- default_server: http://agent.aigenora.com -->
<!-- Built-in servers: production = http://agent.aigenora.com, staging = http://test.aigenora.com, local = http://localhost:41000 -->

### Data Directory

<!-- Example: User uses a fixed data directory -->
<!-- default_data_dir: D:/agents/aigenora-data -->

### Web UI Launch Mode

<!-- Behavior preference for the broadcast service when running in daemon mode (host --daemon / join --daemon). -->
<!-- Values: off (do not spawn broadcast, default), auto (spawn broadcast + open browser), headless (spawn broadcast but no browser) -->
<!-- After reading this, the user-Agent should append the corresponding flag when invoking host/join: -->
<!--   off      → no extra flag (default) or --no-web / --web off -->
<!--   auto     → --web-on      or --web auto -->
<!--   headless → --no-browser  or --web headless -->
<!-- Example: User wants a visual live page every time -->
<!-- web_ui: auto -->
<!-- Example: User wants pure CLI, no web broadcast at all (this is the default) -->
<!-- web_ui: off -->
<!-- WARNING — Agent behavior when this field is absent: see SKILL.md "Agent Decision Rules". -->
<!--   In short: default is off (pure CLI); check context → infer from environment → -->
<!--   ask once "want to open it?" if unclear → 2 consecutive identical choices are -->
<!--   treated as a long-term preference and appended here (this field only). -->

### Remote Protocol UI Acceptance

<!-- Whether to accept a protocol author's distributed UI bundle (third-party web code; trojan risk). -->
<!-- Values: ask (default, ask the human user each time), always (accept automatically), never (never accept; use CLI or self-built UI) -->
<!-- When the user makes a first explicit choice, the Agent persists it here (this field only). -->
<!-- Example: User trusts the community and accepts all published UI -->
<!-- accept_remote_ui: always -->
<!-- Example: User is security-cautious and never accepts third-party UI -->
<!-- accept_remote_ui: never -->

## Protocol Preferences

<!-- Record user preferences for specific protocols -->

### Favorite Protocols

<!-- Example: User likes RPS most, then Guess Number -->

### Disliked Protocols

<!-- Example: User does not want to play Weak Wins All -->

### Default Profiles

<!-- Example: RPS always uses standard profile -->

### Protocol Creation Preferences

<!-- When creating a new business protocol, the Agent reads this first to decide whether to guide the user or choose defaults automatically. -->
<!-- Values: fast-guided (default, ask at most 3 necessary questions), guided (detailed setup), auto (Agent chooses conservative defaults) -->
<!-- protocol_creation_mode: fast-guided -->

<!-- Optional: default template for new protocols. If absent, Agent chooses from intent: turn-based-game / qna-service / bidding. -->
<!-- protocol_creation_template: turn-based-game -->

<!-- Optional: default invitation business direction. If absent, Agent chooses from user intent: supply / demand / chat. -->
<!-- protocol_creation_type: supply -->

<!-- Optional: common defaults. Agent may use these directly when the user says "you decide". -->
<!-- protocol_creation_defaults: {"rounds":3,"max_requests":3,"commit_reveal":"auto","question_depth":"minimal"} -->

<!-- Optional: whether the Agent should continue automatically after draft creation: write hooks.py, run protocol test, register spec, host invitation. -->
<!-- protocol_creation_autorun: {"write_hooks":true,"test":true,"register":false,"host":false} -->

## Behavioral Habits

<!-- Record user interaction habits -->

### Interaction Style

<!-- Example: User prefers concise output, no step-by-step explanations -->
<!-- Example: User wants score reported after each round -->

### Decision Mode

<!-- Example: User wants to choose RPS moves manually, no auto-play -->
<!-- Example: Use binary search strategy for Guess Number -->

### Rating Habits

<!-- Example: User always rates opponents 5 unless they cheat -->

## Custom Notes

<!-- Free-form notes. The Agent can reference anything here. -->
