---
name: weather-lookup
description: Look up current weather conditions and a short forecast for any place the user asks about.
allowed-tools: http_request
---
# Weather Lookup

When the user asks about the weather — current conditions, today's forecast, or a short outlook (next 1–3 days) — use the Open-Meteo public API via the `http_request` tool. Open-Meteo is free and requires no API key.

## Two-step lookup

Weather queries need a latitude/longitude pair. Resolve a free-text location to coordinates first, then fetch the forecast.

### Step 1 — Geocode the place name

Call `http_request` with:
- method: `GET`
- url: `https://geocoding-api.open-meteo.com/v1/search?name=<url-encoded place>&count=1&language=<en|zh>&format=json`

From the response, take the first item in `results[]` and grab `latitude`, `longitude`, `name`, `country`. If `results` is empty, tell the user you couldn't find that place and ask them to rephrase.

### Step 2 — Fetch the forecast

Call `http_request` with:
- method: `GET`
- url: `https://api.open-meteo.com/v1/forecast?latitude=<lat>&longitude=<lon>&current=temperature_2m,relative_humidity_2m,weather_code,wind_speed_10m&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max&timezone=auto&forecast_days=3`

## Presenting the result

- Lead with the place name the user asked about (using the geocoder's canonical name and country).
- Current temperature in °C, a one-word description of `weather_code` (Clear, Cloudy, Rain, Snow, Fog, Thunderstorm — or map from the WMO code table), humidity %, wind speed km/h.
- Then a 3-day outlook: each day's high/low (°C) and brief description.
- Keep the whole reply under ~6 lines. No tables — the chatbot renders markdown lists fine.

## Rules

- Never fabricate numbers. If either API call fails, say the lookup failed and offer to try again.
- Never report a temperature you didn't read from the API response.
- If the user's language is Chinese, reply in Chinese. If English, reply in English.
- Don't call this skill for the user's own device queries — it's only for external weather.
