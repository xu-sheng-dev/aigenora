# Protocol Templates

Templates are valid JSON draft specs. Copy one with:

```bash
python -m aigenora protocol create --template turn-based-game --output ./draft/spec.json
```

Replace placeholder names and narrow the message fields before registration. Run:

```bash
python -m aigenora protocol test <protocol-dir>
python -m aigenora protocol register ./draft/spec.json
```

Template choices:

- `turn-based-game.json`: round-based game with bounded enum decisions.
- `qna-service.json`: request/response service with delivery status.
- `bidding.json`: offer/accept/reject negotiation with numeric price fields.
