# DayMate

![tag:innovationlab](https://img.shields.io/badge/innovationlab-3D8BD3)
![tag:hackathon](https://img.shields.io/badge/hackathon-5F43F1)

Plans your day in any Indian city from one sentence: live weather, real places, events, navigation, calendar and cab links. Multi-agent, with interactive cards and Pro payments.

## Try
- `hi` – trip ideas with live weather
- *Plan my Saturday in Jaipur, I love forts and street food*
- *3 days in Udaipur for 4 friends, ₹1500 each, Hinglish mein*
- `swap 3`, `watch`, `save`

## How it works
An orchestrator (this agent) uses ASI1 to understand the request and fans out to three specialist agents – DayMate Weather, DayMate Places and DayMate Events – then writes a weather-aware, crowd-aware plan with Google Maps navigation, calendar and Uber links, drive times and a budget split. Multi-day trips are DayMate Pro via the Agent Payment Protocol (Stripe).

Code: https://github.com/prajyot2003/daymate
