# DayMate — weather-aware day planner on ASI:One

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Tell DayMate *"Plan my Saturday in Jaipur, I love forts and street food"* inside ASI:One and it returns a timed
itinerary built from **real places** and the **live hourly forecast**. Outdoor stops go in the best hours, and the plan moves indoors when
rain or heat is coming. Follow-ups like *"make it tomorrow instead"* adjust the last plan.

**Problem:** planning a day out means juggling maps, weather apps and opening hours.
**Users:** travellers, students on a weekend trip, anyone visiting a new city.
**Outcome:** a ready-to-follow plan with clickable map links, built from live data.

## Agents

| Agent | Role | Address |
|---|---|---|
| `daymate` (orchestrator) | Chat Protocol / ASI:One entry point. ASI1 parses intent, the agent fans out to the specialists, then ASI1 composes the plan | `agent1q27u5s2hueg8u7ecza8p05ce25frp2jnwchh44f0qepscah29ft96262zge` |
| `daymate-weather` | Hourly forecast from Open-Meteo | `agent1q09f447uf8l648cltdqlzd2785mfd9ensc48mwt8twz3jnfc7pwj5z3zkgw` |
| `daymate-places` | Real places and opening hours from OpenStreetMap (Nominatim) | `agent1qggv7mx7a5dcpllc5lz0nfwtgatzf7f7k2f25f4gc87sg322vp94wwjgray` |

```
ASI:One ──ChatMessage──▶ daymate ──ASI1: intent──▶ {city, day, interests}
                           ├─ WeatherRequest ─▶ daymate-weather ─▶ Open-Meteo
                           ├─ PlacesRequest  ─▶ daymate-places  ─▶ OpenStreetMap
                           └─ ASI1: compose itinerary ─▶ reply in ASI:One
```

The specialists run in parallel. If one fails or times out, the plan is still produced with what is available.

## Run

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env    # add ASI1_API_KEY + three random seeds
python daymate.py --check "Plan my Saturday in Jaipur, I love forts and street food"   # end-to-end self-check
python daymate.py       # start all three agents
```

Open the **Agent inspector** link from the logs, then choose **Connect → Mailbox**. Open the agent profile and click **Chat with Agent**
to talk to it in ASI:One.

Only an [ASI:One API key](https://asi1.ai/dashboard/api-keys) is needed. Open-Meteo and OpenStreetMap need no keys.

## Links

- ASI:One shared chat: _TODO_
- Agentverse profile: _TODO_
- Demo video: _TODO_
