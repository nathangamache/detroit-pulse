# ── Feed context lookup ──────────────────────────────────────────────────────

FEED_CONTEXT = {
    "wayneco_detroit_police_fire": {
        "name":         "Detroit Police and Fire",
        "primary_city": "Detroit",
        "county":       "Wayne County",
        "area_note":    "Covers City of Detroit. Streets include Woodward, Gratiot, Mound, Van Dyke, Jefferson, Michigan Ave, Grand River, Livernois, Outer Drive. Mile roads from 6 Mile to 8 Mile.",
    },
    "wayneco_detroit_police_dispatch": {
        "name":         "Detroit Police Dispatch",
        "primary_city": "Detroit",
        "county":       "Wayne County",
        "area_note":    "Covers all Detroit precincts. Same geography as Detroit Police and Fire feed.",
    },
    "wayneco_detroit_fire": {
        "name":         "Detroit Fire",
        "primary_city": "Detroit",
        "county":       "Wayne County",
        "area_note":    "Detroit Fire Department. Covers City of Detroit.",
    },
    "wayneco_detroit_ems": {
        "name":         "Detroit EMS Dispatch",
        "primary_city": "Detroit",
        "county":       "Wayne County",
        "area_note":    "Detroit EMS. Covers City of Detroit.",
    },
    "wayneco_downriver": {
        "name":         "Downriver Public Safety",
        "primary_city": "Downriver area",
        "county":       "Wayne County",
        "area_note":    "Covers Downriver communities south of Detroit: Wyandotte, Trenton, Riverview, Southgate, Allen Park, Lincoln Park, Melvindale, Ecorse, River Rouge, Taylor, Romulus. Key roads: Eureka, Fort, Allen, Dix, Telegraph.",
    },
    "wayneco_westland_gardencity": {
        "name":         "Westland-Garden City Police and Fire",
        "primary_city": "Westland",
        "county":       "Wayne County",
        "area_note":    "Covers Westland and Garden City. Key roads: Ford Road, Cherry Hill, Warren, Merriman, Wayne Road, Middlebelt.",
    },
    "wayneco_dearborn": {
        "name":         "Dearborn Police and Fire",
        "primary_city": "Dearborn",
        "county":       "Wayne County",
        "area_note":    "Covers Dearborn and Dearborn Heights. Key roads: Michigan Ave, Ford Road, Schaefer, Greenfield, Telegraph, Outer Drive.",
    },
    "wayneco_grossepointe": {
        "name":         "Grosse Pointes and Harper Woods Police and Fire",
        "primary_city": "Grosse Pointe",
        "county":       "Wayne County",
        "area_note":    "Covers Grosse Pointe Park, Grosse Pointe City, Grosse Pointe Farms, Grosse Pointe Woods, Grosse Pointe Shores, and Harper Woods. Key roads: Mack, Jefferson, Kercheval, Moross.",
    },
    "wayneco_plymouthnorthville": {
        "name":         "Plymouth-Northville Area Public Safety",
        "primary_city": "Plymouth",
        "county":       "Wayne County",
        "area_note":    "Covers Plymouth Township, Plymouth City, Northville Township, Northville City, and Canton Township. Key roads: Plymouth Road, Ann Arbor Road, Five Mile, Six Mile, Seven Mile, Sheldon, Beck, Haggerty, Ridge, Napier. Forest Avenue is in Plymouth. Do NOT default to Royal Oak or Oakland County for this feed.",
    },
    "wayneco_southwestern": {
        "name":         "Southwestern Wayne County Police and Fire",
        "primary_city": "Van Buren Township",
        "county":       "Wayne County",
        "area_note":    "Covers Van Buren Township, Belleville, Sumpter Township, Huron Township. Key roads: Belleville Road, Rawsonville, Huron River Drive, Hannan, Savage.",
    },
    "wayneco_public_safety": {
        "name":         "Wayne County Public Safety",
        "primary_city": "Wayne County",
        "county":       "Wayne County",
        "area_note":    "County-wide feed covering multiple Wayne County agencies. Use cross streets and context to determine most likely city.",
    },
    "wayneco_romulus": {
        "name":         "Romulus-Huron Township Police and Fire",
        "primary_city": "Romulus",
        "county":       "Wayne County",
        "area_note":    "Covers Romulus and Huron Township near Detroit Metropolitan Airport. Key roads: Eureka, Goddard, Wick, Wayne Road, Middlebelt.",
    },
    "wayneco_northville_plymouth_city": {
        "name":         "Northville Police and Fire / Plymouth City Fire",
        "primary_city": "Northville",
        "county":       "Wayne County",
        "area_note":    "Covers Northville City and Plymouth City. Key roads: Main Street, Center, Griswold, Wing, Ann Arbor Trail.",
    },
    "wayneco_franklin_bingham": {
        "name":         "Franklin-Bingham Fire Dispatch",
        "primary_city": "Franklin",
        "county":       "Oakland County",
        "area_note":    "Covers Franklin and Bingham Farms. Key roads: Franklin Road, Fourteen Mile, Northwestern Highway.",
    },
    "oaklandco_royaloak_fire": {
        "name":         "Royal Oak Fire",
        "primary_city": "Royal Oak",
        "county":       "Oakland County",
        "area_note":    "Covers Royal Oak. Key roads: Woodward Avenue (11 Mile to 14 Mile in this area), Main Street, 11 Mile Road, 12 Mile Road, 13 Mile Road, 14 Mile Road, Crooks Road, Campbell Road, Rochester Road. Woodward and 9 Mile is Ferndale/Oak Park NOT Royal Oak. Woodward and 11 Mile through 14 Mile is Royal Oak.",
    },
    "washtenaw_metro": {
        "name":         "Washtenaw County Metro Police and Fire",
        "primary_city": "Ann Arbor",
        "county":       "Washtenaw County",
        "area_note":    "Covers Ann Arbor, Ypsilanti, Saline, Chelsea, Milan, and surrounding Washtenaw County areas. Key roads: Washtenaw, Packard, Michigan Ave, State, Plymouth Road, Jackson, Dexter.",
    },
    "washtenaw_livingston": {
        "name":         "Livingston County Public Safety",
        "primary_city": "Howell",
        "county":       "Livingston County",
        "area_note":    "Covers Livingston County including Howell, Brighton, Hartland, Fowlerville. Key roads: Grand River, Brighton Road, M-59.",
    },
}

FEED_CONTEXT_DEFAULT = {
    "name":         "Metro Detroit Public Safety",
    "primary_city": "Metro Detroit",
    "county":       "Wayne County",
    "area_note":    "Metro Detroit area. Use cross streets and context to determine most likely city.",
}


def get_feed_context(feed_id: str) -> dict:
    return FEED_CONTEXT.get(feed_id, FEED_CONTEXT_DEFAULT)


def format_feed_context(feed_id: str) -> str:
    ctx = get_feed_context(feed_id)
    return (
        f"Feed: {ctx['name']}\n"
        f"Primary city/area: {ctx['primary_city']}\n"
        f"County: {ctx['county']}\n"
        f"Geographic context: {ctx['area_note']}"
    )


# ── Pass 1 — Address Normalization ──────────────────────────────────────────

NORMALIZE_SYSTEM = """You are a Detroit metro area dispatch address normalizer.
Convert dispatch shorthand into full, geocodable location strings.

Metro Detroit geography reference:
- Mile roads run east-west: 6 Mile, 7 Mile, 8 Mile (state line),
  9 Mile, 10 Mile, 11 Mile, 12 Mile, 13 Mile, 14 Mile, 15 Mile
- Major north-south corridors: Woodward Avenue, Gratiot Avenue, Mound Road,
  Van Dyke Avenue, Dequindre Road, John R Street, Schoenherr Road,
  Beeline Highway, Livernois Avenue, Outer Drive, Harper Avenue
- Major diagonal / east-west roads: Northwestern Highway, Telegraph Road,
  Ford Road, Michigan Avenue, Ecorse Road, Cherry Hill Road,
  Plymouth Road, Tireman Avenue, Vernor Highway, Ann Arbor Road
- "Northwestern" alone = Northwestern Highway
- "Lahser" = Lahser Road (Oakland/Wayne county line area)
- "Gratiot" = Gratiot Avenue (locals pronounce it "Grashit")
- "Mound" = Mound Road
- "Telegraph" = Telegraph Road
- "8 Mile" = 8 Mile Road

IMPORTANT: Use the feed context below to determine the most likely city
for any ambiguous address. The feed's primary city and area note should
be your first assumption when the city is not explicitly stated.

{feed_context}

CRITICAL — NUMBER DISAMBIGUATION:
Numbers in dispatch audio describe many things besides streets. You MUST
determine from context whether a number refers to a location or something else.

Numbers that are NEVER part of an address:
- Round counts: "6 rounds", "shots fired", "six rounds fired"
- Floor numbers when describing where someone is: "20th floor", "floor 4"
- Unit/badge numbers: "Unit 1344", "Car 42", "Engine 30"
- Frequencies or channels: "go to channel 3", "switch to tac 6"
- Times: "at 1340", "at approximately 13:40"
- Counts of people: "3 males", "two victims"
- Alarm levels: "second alarm", "box alarm 3"

EXAMPLES OF WHAT NOT TO DO:
BAD:  "6 rounds fired at 14303 East Warren" -> "14303 6 Mile Road, Detroit, MI"
      WRONG: "6" describes rounds fired, not a street. The address is 14303 East Warren.
GOOD: "6 rounds fired at 14303 East Warren" -> "14303 East Warren Avenue, Detroit, MI"

BAD:  "trapped on the 20th floor at Riverfront" -> "20th Floor Road, Detroit, MI"
      WRONG: "20th floor" is a floor number, not a street.
GOOD: "trapped on the 20th floor at Riverfront" -> "Riverfront Plaza, Detroit, MI"

BAD:  "three victims at 9 Mile and Woodward" -> "3 9 Mile Road, Detroit, MI"
      WRONG: "three" describes victims, not an address.
GOOD: "three victims at 9 Mile and Woodward" -> "9 Mile Road and Woodward Avenue, Royal Oak, MI"

RULE: A number is part of an address ONLY when it is immediately preceded or
followed by a street name or clear address context. If a number appears next
to words like "rounds", "shots", "floor", "unit", "car", "engine", "victims",
"males", "alarm", "channel", "tac", or a time value — it is NOT address data.

LOCATION TYPES — dispatch audio contains many types of locations, not just
street intersections. Recognize each type and format accordingly:

TYPE 1 — Street address: "24400 Northwestern" -> "24400 Northwestern Highway, Southfield, MI"
TYPE 2 — Intersection: "Woodward and 9" -> "Woodward Avenue and 9 Mile Road, Royal Oak, MI"
TYPE 3 — Named place (apartment complex, park, school, hospital, business, landmark, casino):
  Do NOT invent a street address. Return the place name plus city.
  Format: "[Place Name], [City], MI"
  Examples:
  - "Waterford Bend" (apartment complex in Plymouth area) -> "Waterford Bend Apartments, Plymouth, MI"
  - "Stevenson High School" -> "Stevenson High School, Livonia, MI"
  - "Beaumont Hospital" -> "Beaumont Hospital, Royal Oak, MI"
  - "Ford Field" -> "Ford Field, Detroit, MI"
  - "Maybury State Park" -> "Maybury State Park, Northville, MI"
  - "Chandler Park" -> "Chandler Park, Detroit, MI"
  - "Palmer Park" -> "Palmer Park, Detroit, MI"
  - "Rouge Park" -> "Rouge Park, Detroit, MI"
  - "Belle Isle" -> "Belle Isle Park, Detroit, MI"
  - "MGM" or "MGM Casino" -> "MGM Grand Detroit Casino, Detroit, MI"
  - "Greektown Casino" -> "Greektown Casino, Detroit, MI"
  - "MotorCity Casino" or "Motor City Casino" -> "MotorCity Casino, Detroit, MI"
  - "LCA" or "Little Caesars Arena" -> "Little Caesars Arena, Detroit, MI"
  - "Comerica" or "Comerica Park" -> "Comerica Park, Detroit, MI"
  - "Ren Cen" or "Renaissance Center" -> "Renaissance Center, Detroit, MI"
  - "DMC" -> "Detroit Medical Center, Detroit, MI"
  - "DTW" or "Metro Airport" -> "Detroit Metropolitan Wayne County Airport, Romulus, MI"
TYPE 4 — Block reference: "7400 block of Gratiot" -> "7400 Gratiot Avenue, Detroit, MI"
TYPE 5 — Highway reference: "I-275 near Five Mile" -> "I-275 near Five Mile Road, Plymouth, MI"

KEY RULE: If the location sounds like a named place (ends in common suffixes
like Bend, Landing, Commons, Place, Park, Center, Plaza, Manor, Village,
Crossing, Pointe, Ridge, Glen, Woods, Lakes, Club, Academy, School,
Hospital, Mall, Station) — treat it as TYPE 3. Do NOT append road names
to place names.

Instructions:
- Return ONLY the normalized location string, nothing else
- Default to the feed's primary city when city is ambiguous
- If no address or location is detectable, return exactly: NO_LOCATION
- Do not explain, do not add punctuation beyond the location string itself"""

NORMALIZE_USER = "Transcript: {transcript}"


# ── Pass 2 — Structuring + Correlation ──────────────────────────────────────

STRUCTURE_SYSTEM = """You are a Detroit metro area dispatch intelligence parser.
You are given a raw dispatch transcript, a normalized address, a confirmed
geocoded location, the source feed details, and a snapshot of all currently
active incidents on this feed.

Your job:
1. Determine if this transcript is actionable (has_incident)
2. Decide the correlation action: NEW, UPDATE, RESOLVE, or UNASSOCIATED
3. Return a single valid JSON object — no preamble, no markdown, no explanation

JSON schema (return exactly this structure):
{{
  "has_incident": boolean,
  "correlation_action": "NEW|UPDATE|RESOLVE|UNASSOCIATED",
  "incident_id": "existing incident_id if UPDATE or RESOLVE, else null",
  "incident_type": "STRUCTURE_FIRE|VEHICLE_FIRE|MEDICAL|TRAFFIC_ACCIDENT|SHOOTING|ASSAULT|DOMESTIC|ROBBERY|BURGLARY|THEFT|USE_OF_FORCE|PRISONER|PURSUIT|BOMB_THREAT|HAZMAT|WELFARE_CHECK|SUSPICIOUS|OTHER|UNKNOWN",
  "priority": "HIGH|MEDIUM|LOW|UNKNOWN",
  "units_added": ["array of unit IDs newly mentioned"],
  "units_cleared": ["array of unit IDs going clear or back in service"],
  "summary_update": "one sentence describing what is new or confirmed in this chunk"
}}

Incident type guidance:
- STRUCTURE_FIRE: building/structure on fire, fire alarm, smoke in structure
- VEHICLE_FIRE: car, truck, or other vehicle on fire
- MEDICAL: EMS, medical emergency, injury, cardiac, overdose, unconscious person
- TRAFFIC_ACCIDENT: vehicle crash, MVA, hit and run
- SHOOTING: shots fired, gunshot victim, person shot
- ASSAULT: physical altercation, battery, person attacked (non-shooting)
- DOMESTIC: domestic disturbance, family fight, domestic violence
- ROBBERY: armed robbery, strong-arm robbery, carjacking
- BURGLARY: breaking and entering, home invasion, B&E
- THEFT: larceny, shoplifting, stolen property (no force)
- USE_OF_FORCE: officer use of force report, in-custody force, DDC/jail incident
- PRISONER: prisoner transport, in-custody medical, jail/detention center call
- PURSUIT: vehicle pursuit, foot pursuit, fleeing suspect
- BOMB_THREAT: bomb threat, suspicious package, explosive device
- HAZMAT: hazardous materials, gas leak, chemical spill
- WELFARE_CHECK: wellness check, person not responding, check the welfare
- SUSPICIOUS: suspicious person, suspicious vehicle, suspicious activity
- OTHER: real incident but doesn't fit above categories
- UNKNOWN: cannot determine incident type from transcript

Correlation rules:
- If a unit in this transcript matches a unit in an active incident -> UPDATE
- If transcript has dispatch language with a new location -> NEW
- If a unit goes clear/back in service -> RESOLVE
- If uncertain -> UNASSOCIATED

Set has_incident=false for: radio tests, signal checks, administrative
traffic, weather updates, unit-to-unit chatter with no incident reference.

Feed context:
{feed_context}"""

STRUCTURE_USER = """Transcript: {transcript}
Normalized address: {normalized_address}
Confirmed location: {geocoded_address} ({lat}, {lng})
County: {county}
Feed: {feed_id}

Active incidents (this feed + nearby cross-feed incidents for mutual aid awareness):
{active_incidents}"""


# ── Active incidents context formatter ──────────────────────────────────────

def format_active_incidents(incidents: list[dict]) -> str:
    if not incidents:
        return "None"
    lines = []
    for inc in incidents:
        units = ", ".join(inc.get("units", []))
        lines.append(
            f"- ID={inc['incident_id']} | "
            f"type={inc['incident_type']} | "
            f"location={inc.get('address_full', 'unknown')} | "
            f"units=[{units}] | "
            f"opened={inc.get('opened_at', 'unknown')}"
        )
    return "\n".join(lines)


# ── LLM Correlation Judge ──────────────────────────────────────────────────

CORRELATION_JUDGE_SYSTEM = """You are a Detroit metro area public safety dispatch correlation engine.
Your job is to determine whether a new radio transcript chunk describes the same
real-world incident as one of the currently active incidents, or whether it is
a new separate event.

Consider all available evidence:
- Location: same address, nearby address, or related locations (e.g. a hospital
  receiving a patient from an incident location)
- Incident type: same type (fire, shooting, medical) is strong evidence
- Units: shared unit IDs or units from same station
- Time: events close in time are more likely related
- Context: narrative continuity (e.g. "second alarm" implies a fire already started,
  "patient en route" implies an earlier incident)
- Cross-feed: different radio feeds can cover the same incident

Be conservative — only match if you are reasonably confident they are the same event.
A false negative (creating a duplicate) is better than a false positive (merging
unrelated events).

Respond with JSON only, no explanation:
{
  "match": true | false,
  "incident_id": "the matching incident_id if match=true, else null",
  "confidence": "HIGH | MEDIUM | LOW",
  "reason": "one sentence explaining your decision"
}"""


CORRELATION_JUDGE_USER = """New transcript chunk:
Feed: {feed_id}
Transcript: {transcript}
Normalized address: {normalized_address}
Geocoded location: {geocoded_address}
Incident type: {incident_type}
Units mentioned: {units}
Time: {timestamp}

Active incidents to compare against:
{active_incidents_text}

Does this chunk describe the same event as any active incident, or is it new?"""