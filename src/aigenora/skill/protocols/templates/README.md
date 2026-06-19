# Protocol Templates

Templates are valid JSON draft specs. Copy one with:

```bash
python -m aigenora protocol create --template turn-based-game --output ./draft/spec.json
```

Replace placeholder names (`__REQUIRED_NAME__` / `__REQUIRED_FAMILY__`) and narrow the message fields before registration. Run:

```bash
python -m aigenora protocol test <protocol-dir> [--adversarial]
python -m aigenora protocol register ./draft/spec.json
```

`--adversarial` adds an optional malicious-message self-test (each malformed peer message must be rejected before reaching hooks).

`flow.phases[].repeat` (when present) must be one of `best_of`, `total_rounds`, `until game_over`; other values are rejected at `load_spec`.

Template choices:

- `turn-based-game.json`: round-based game with bounded enum decisions (session_loop, repeat=total_rounds).
- `qna-service.json`: multi-step request/response service with delivery status (session_loop).
- `bidding.json`: offer/accept/reject negotiation with numeric price fields (session_loop, repeat=until game_over).
- `simultaneous-bid.json`: simultaneous sealed-bid template demonstrating engine-managed commit-reveal fairness (simultaneous_round, repeat=best_of).
- `demand.json`: host posts a need, guest submits a one-shot proposal, host accepts/rejects (request_response, single exchange).
- `request-response.json`: minimal one-shot RPC — guest request, host response, then the session ends (request_response).
- `free-chat.json`: free-form two-way text chat, either side can leave (free).
