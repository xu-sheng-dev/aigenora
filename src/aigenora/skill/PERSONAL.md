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
<!-- Values: off (do not spawn broadcast), auto (spawn broadcast + open browser), headless (spawn broadcast but no browser) -->
<!-- After reading this, the user-Agent should append the corresponding flag when invoking host/join: -->
<!--   off      → --no-web or --web off -->
<!--   auto     → --web-on      or --web auto -->
<!--   headless → --no-browser  or --web headless -->
<!-- Example: User wants a visual live page every time -->
<!-- web_ui: auto -->
<!-- Example: User wants pure CLI, no web broadcast at all -->
<!-- web_ui: off -->
<!-- WARNING — Agent behavior when this field is absent: see SKILL.md "Agent Decision Rules". -->
<!--   In short: human daemon defaults to auto; autonomous/hybrid default to off. Then check context/environment → -->
<!--   ask once "want to open it?" if unclear → 2 consecutive identical choices are -->
<!--   treated as a long-term preference and appended here (this field only). -->

### Platform-Published Remote Protocol UI

<!-- Whether to accept an immutable UI bundle published by the protocol author to the community platform (third-party web code; trojan risk). -->
<!-- Values: ask (default, ask the human user each time), always (accept automatically), never (never accept; use CLI or self-built UI) -->
<!-- When the user makes a first explicit choice, the Agent persists it here (this field only). -->
<!-- Example: User trusts the community and accepts all published UI -->
<!-- accept_remote_ui: always -->
<!-- Example: User is security-cautious and never accepts third-party UI -->
<!-- accept_remote_ui: never -->

### Host P2P UI Acceptance

<!-- When neither local nor platform UI is available, whether to obtain a UI snapshot from this match's Host over P2P. This is riskier than platform-published UI and is a separate decision. -->
<!-- Values: ask (default, ask when actually useful), always (allow P2P fallback automatically), never (never accept Host UI) -->
<!-- accept_host_ui_p2p: ask -->

### Share Local UI with Guests

<!-- As Host, whether to use --share-ui to offer local protocol_dir/ui/ to Guests that explicitly accept it. -->
<!-- Values: ask (default, confirm while preparing the invitation), always (share whenever UI exists), never (never share) -->
<!-- share_ui_with_guests: ask -->

### Host P2P Executable Bundle Acceptance

<!-- Whether to accept this session's Host-provided hooks.py + ui/ bundle with --accept-host-bundle. -->
<!-- This runs Host Python in a restricted per-session subprocess. It is NOT a security sandbox and is appropriate only for a Host the user explicitly trusts. UI consent never implies this consent. -->
<!-- Values: ask (default, require a risk confirmation for the current Host/session), always (only when the user has explicitly authorized all trusted Hosts), never (never execute Host Python) -->
<!-- accept_host_bundle_p2p: ask -->

### Share Executable Bundle with Guests

<!-- As Host, whether to use --share-bundle to offer local hooks.py + ui/ to Guests that independently opt in. -->
<!-- Values: ask (default, include the executable-code risk in the invitation confirmation), always (offer whenever the bundle passes validation), never (never offer executable Python) -->
<!-- share_bundle_with_guests: ask -->

## Protocol Preferences

<!-- Record user preferences for specific protocols -->

### Favorite Protocols

<!-- Example: User likes RPS most, then Guess Number -->

### Disliked Protocols

<!-- Example: User does not want to play Weak Wins All -->

### Default Profiles

<!-- Example: RPS always uses standard profile -->

### Invitation Setup and Confirmation Preferences

<!-- These fields control hosting an invitation with an existing protocol. Do not confuse them with creating a new protocol below. -->
<!-- Missing fields: ask-material (default; ask only about omissions that change intent) / infer-preferences (prefer established preferences). -->
<!-- invitation_setup_mode: ask-material -->

<!-- Parameter authority: confirm-all (default; Agent only organizes) / agent-choose-defaults (Agent may choose routine defaults such as rounds and TTL). -->
<!-- invitation_parameter_authority: confirm-all -->

<!-- Final approval: always (default; show the plain-language confirmation card and wait) / standing-authorized (an explicit standing authorization already exists). -->
<!-- standing-authorized must come from explicit user authorization; never infer it from repeated approvals. -->
<!-- invitation_final_approval: always -->

<!-- Common invitation defaults; record only genuinely stable preferences. -->
<!-- default_control_mode: human -->
<!-- default_invitation_type: supply -->
<!-- default_invitation_ttl_minutes: 30 -->
<!-- default_share_ui: false -->
<!-- invitation_defaults_by_protocol: {"rock-paper-scissors":{"best_of":3}} -->

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
