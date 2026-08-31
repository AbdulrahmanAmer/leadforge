# ICP interview guide + answers.yaml schema

## Question bank (adapt tone; batch 2–3 per message; skip anything already stated)

1. **Offer:** "What exactly are you selling/offering, in one sentence? What's the outcome for the client?"
2. **Category:** "What type of businesses should I hunt for? Any sub-types to include or avoid?" (map to Google Maps category phrasing:
   'auto repair shop', 'real estate agency', 'dental clinic', …)
3. **Geography — ALWAYS ask the country:** "Which country, and which city/area within it?" Get an ISO2
   country code (US, GB, EG, AE, …) plus a specific area. In federal countries (US/CA/AU/BR/IN/MX) insist on
   the state/region too: "Houston, TX". Reject vague answers ("the Gulf", "downtown", "up north") and ask
   which named city — intake will error without a country, and geocoding refuses ambiguous matches rather
   than guessing, so a vague answer just costs a round trip.
4. **Size band:** "Any size filter — minimum review count, solo operators ok, chains ok?"
5. **Hard disqualifiers:** "What makes a business an automatic NO? (franchise, no phone, already a client, competitor…)"
6. **Soft qualifications:** "What makes one a GREAT fit for the offer? (e.g. for web design: outdated/no website; for booking software:
   busy but phone-only)" — these become scoring `needs` signals.
7. **Decision maker:** "Who do you want to reach — Owner? GM? Marketing? In priority order."
8. **Volume + region profile:** "How many leads do you want (default 200)? Outreach region for the compliance note: us/uk/eu?"

Defaults if user shrugs: size=none, hard=[no_phone], dm titles=[Owner, General Manager, Manager], caps.max_leads=200, region=us.

## answers.yaml (write exactly this shape; intake validates)

```yaml
version: 1
campaign: short-slug-you-invent          # kebab, unique per campaign
offer:
  what: "Website redesign + booking system for auto repair shops"
  value_prop: "More booked jobs, fewer missed calls"
  sender: "Striker's agency"
target:
  categories: ["auto repair shop", "transmission shop"]   # 1-5, Maps-style phrasing
  geography:
    country: US                          # REQUIRED, ISO2 — ask the user; never guess
    areas: ["Houston, TX"]               # 1-3 specific place names; OR bbox: [minLng,minLat,maxLng,maxLat]
  size:
    min_reviews: 10                      # or null
qualify:
  hard: [franchise_or_chain, no_phone]   # from vocabulary below; unknown terms rejected
  soft: [website_missing, low_rating_high_volume, stale_site]
decision_maker:
  titles_priority: ["Owner", "General Manager", "Service Manager"]
caps:
  max_leads: 200
compliance:
  region_profile: us                     # us | uk | eu
notes: "free text from the user worth keeping"
```

### Qualifier vocabulary (v0.1)

hard: `franchise_or_chain` · `no_phone` · `no_website_hard` (exclude sites-less businesses — only when the offer NEEDS a site) ·
`closed_or_unverified` · `competitor:<name-substring>` · `existing_client:<name-substring>`

soft (need signals; each maps to a scoring signal + hook template): `website_missing` · `website_no_ssl` · `stale_site` (old copyright/
broken pages) · `low_rating_high_volume` (rating ≤ 3.9 & reviews ≥ 50) · `few_reviews` (< 10) · `weak_social_presence` · `phone_only_booking`
(no online booking detected) · `hiring` (careers page present)

If the user's criterion fits no vocabulary term, put it in `notes` and tell them scoring can't automate it yet (it can still guide DM
labeling and their outreach).

## Worked examples

- **Web agency → auto repair, Houston** (above): `website_missing`/`stale_site` are POSITIVE need signals; scoring handles the inversion
  automatically via the ICP — never "fix" it by hand.
- **B2B SaaS (booking tool) → dental clinics, 3 cities:** categories: ["dental clinic","orthodontist"]; soft: [phone_only_booking,
  low_rating_high_volume]; dm: ["Practice Manager","Owner","Office Manager"].
- **Commercial realtor → growing SMBs:** categories: ["coworking space","gym","daycare"]; soft: [hiring]; hard: [franchise_or_chain];
  dm: ["Owner","Founder","Managing Director"].
