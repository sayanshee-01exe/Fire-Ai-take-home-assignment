"""Corruption injector: the "red team" that plants each of the 12 problems into a clean batch.

Why inject at all: detection rate, false alarms and explanation quality need ground truth,
and real 311 data has none (nobody labelled which rows are wrong). So every evaluation batch
is a clean batch plus ONE known corruption, and `Injected.row_labels` / `batch_label` are
the answer key the detectors are scored against. DESIGN.md section 3 is the contract.

Choices that keep the answer key honest:
* Wrong by construction. "Inconsistent" is measured on the full year held in
  `InjectionContext`: an injected (ZIP, borough) or (complaint type, descriptor) pair never
  occurs in 2023 (nor in another spelling, see _spelling), and a swapped-in resolution text
  is >= 95 % tied to the other status group (an "open" text on Jan-Aug rows alone too, and in
  its words, see NEUTRAL_OPEN_TEXTS; likewise the "closed" text of a reopened row,
  NEUTRAL_CLOSED_TEXTS). Rows are counted the way data/clean.py
  counts them (a null status or agency still counts), so the sanitiser agrees. A detector
  that flags an injected row is right; a labelled row is never fine.
* Nothing else turns wrong. An injection must not make an unlabelled row an instance of the
  problem: a near-duplicate copy (f) never lands within 300 s before a later, real report of
  the same request, which would make that report look like a copy of the copy.
* Realistic, not convenient. Replacement ZIPs and descriptors are real values drawn in
  proportion to their full-year frequency, so an injected row looks like ordinary traffic
  rather than a planted outlier that any rarity check would catch. k's texts are real too, but
  drawn uniformly among the row agency's own texts: with frequency weights a single frequent
  template dominated the insertions. A copy with a mistyped address (f) is geocoded on its own,
  a few metres from the original, as a real re-entry would be, with latitude/longitude the exact
  projection of its x/y as in every real row.
* Reproducible. Every random choice comes from the caller's numpy Generator, and candidate
  lists come from sorted pandas indexes, never from Python sets (their order changes
  between processes).
* Robust. DESIGN section 7 mixes corruptions, so a batch may already lack a column or hold it
  in another dtype. An injector then uses what is left or injects nothing and says why in
  `params`; it never raises and never writes a dropped column back.
* Independent. Written from the spec table only, without reading the detectors.
"""
from __future__ import annotations

import functools
import hashlib
import re
import string
import warnings
from collections import Counter
from dataclasses import dataclass
from typing import Callable

import numpy as np
import pandas as pd

from project_b import config

# --------------------------------------------------------------------------
# Tunables
# --------------------------------------------------------------------------
# a: plausible new names per contract column. The display-name style of the column
#    ("Incident Zip", "Descriptor") is always one more option.
COLUMN_ALIASES: dict[str, list[str]] = {
    "unique_key": ["request_id", "sr_number"],
    "created_date": ["created_at", "date_created"],
    "closed_date": ["closed_at", "date_closed"],
    "agency": ["agency_code", "dept"],
    "agency_name": ["agency_full_name", "department"],
    "complaint_type": ["problem", "complaint"],  # the NYC portal now labels it "Problem"
    "descriptor": ["problem_detail", "complaint_detail"],
    "descriptor_2": ["additional_details"],
    "location_type": ["location", "place_type"],
    "incident_zip": ["zip_code", "zip", "postal_code"],
    "incident_address": ["address", "street_address"],
    "street_name": ["street"],
    "city": ["incident_city", "town"],
    "status": ["request_status", "state"],
    "due_date": ["due_at", "sla_due_date"],
    "resolution_description": ["resolution", "resolution_text"],
    "resolution_action_updated_date": ["resolution_updated_at", "last_action_date"],
    "community_board": ["cb", "community_district"],
    "borough": ["boro", "borough_name"],
    "x_coordinate_state_plane": ["x_coordinate", "x_coord"],
    "y_coordinate_state_plane": ["y_coordinate", "y_coord"],
    "open_data_channel_type": ["channel", "intake_channel"],
    "park_borough": ["park_boro"],
    "latitude": ["lat"],
    "longitude": ["lon", "lng"],
}

# b, c: a mid-batch switch happens somewhere in this share of the batch.
SWITCH_RANGE = (0.20, 0.80)
NUMERIC_COLUMNS = ["unique_key", "latitude", "longitude", "x_coordinate_state_plane", "y_coordinate_state_plane"]

# c: the date columns that change format, and the formats they change to.
SWITCHED_DATE_COLUMNS = ["created_date", "closed_date", "resolution_action_updated_date"]
DATE_FORMATS = {
    "us_12h": "%m/%d/%Y %I:%M:%S %p",
    "dmy_24h": "%d-%m-%Y %H:%M",
    "iso_space_no_ms": "%Y-%m-%d %H:%M:%S",
    "iso_day_month_swapped": "%Y-%d-%mT%H:%M:%S.000",  # keeps the ISO shape: silent when day <= 12
}

# f: a near-duplicate is the same request entered again a few minutes later.
MAX_TIME_SHIFT_S = 300
MAX_KEY_OFFSET = 500  # new unique_key = original + U(1, MAX_KEY_OFFSET)
# DESIGN section 2: a row is a near-duplicate of an earlier report of the same request
# (agency, complaint type, descriptor, borough, address up to case, spacing and these
# spellings) created at most this many seconds before it.
NEAR_DUPLICATE_WINDOW_S = 300
ADDRESS_SPELLINGS = {"STREET": "ST", "AVENUE": "AVE"}
TEXT_EDITS = {  # edit -> (column it changes, pattern the value must contain for the edit to apply)
    "street_abbrev": ("incident_address", r"\bSTREET\b"),
    "avenue_abbrev": ("incident_address", r"\bAVENUE\b"),
    "double_space": ("incident_address", r" "),
    "typo": ("incident_address", r"[A-Za-z]"),
    "descriptor_case": ("descriptor", r"[A-Za-z]"),
}
# A mistyped address is geocoded on its own, so a 'typo' copy lands this many metres from the
# original in a random direction (rounding x/y to whole feet changes that by at most 0.22 m). The
# other edits (STREET->ST, AVENUE->AVE, a doubled space, descriptor case) geocode to the same point
# and keep the original's coordinates exactly.
TYPO_SHIFT_M = (5.0, 15.0)
METRES_PER_DEGREE = 111_320  # one degree of latitude; a degree of longitude is this x cos(latitude)
US_FEET_PER_METRE = 3937 / 1200  # 3.28083 (1 US survey foot = 1200/3937 m): the state-plane x/y unit (x grows east, y grows north)
# In the source, latitude/longitude are the inverse projection of the whole-foot x/y (to a constant
# 4.4 mm), so a moved copy derives them from its new x/y the same way. The x/y are NAD83 / New York
# Long Island (ftUS), EPSG:2263: Lambert Conic Conformal (2SP) on the GRS80 ellipsoid. Parameters
# from the EPSG registry (EPSG:2263); formulas from IOGP Guidance Note 7-2 (EPSG method 9802).
GRS80_A = 6_378_137.0  # semi-major axis, metres
GRS80_INV_F = 298.257222101  # inverse flattening
LCC_LAT_ORIGIN = 40 + 10 / 60  # latitude of false origin: 40 deg 10' N
LCC_LON_ORIGIN = -74.0  # longitude of false origin: 74 deg 00' W
LCC_STANDARD_PARALLELS = (41 + 2 / 60, 40 + 40 / 60)  # 41 deg 02' N and 40 deg 40' N
LCC_FALSE_EASTING_FT = 984_250.0  # US survey feet (= 300,000 m); the false northing is 0

# g: null spike in one agency's records.
MIN_AGENCY_ROWS = 100
MAX_REFERENCE_NULL_RATE = 0.10  # only columns the agency normally fills
NULL_FRACTION_RANGE = (0.3, 0.9)
NULL_SPIKE_CHOICES = [  # one choice = the columns nulled together (a geocoder outage loses both)
    ("incident_zip",),
    ("incident_address",),
    ("latitude", "longitude"),
    ("descriptor",),
    ("location_type",),
    ("city",),
    ("community_board",),
    ("resolution_description",),
]

# h: closed_date = created_date - lag (whole seconds), or the closed year typed wrong.
LAG_RANGES_S = {"minutes": (60, 59 * 60), "days": (86_400, 30 * 86_400)}
TYPO_YEAR = "2022"  # has no 29 February

# j: catch-all descriptors name no subject, so swapping one in contradicts no complaint type and a
#    detector that judges meaning would be charged a miss for being right: they are never drawn.
#    These are all the catch-alls among the 990 descriptors of Jan-Aug 2023 ('Other' 41,857 rows,
#    'N/A' 36,061, 'Other (complaint details)' 11,660, 'Other/Unknown' 1,956, 'Other (Explain
#    Below)' 824, 'Other - Explain Below' 246, 'General Complaint' 65, 'General Inquiry' 6), plus
#    the bare placeholders later months could bring. Matched whole, in any spelling (_spelling
#    ignores case and punctuation), never by a leading word: 'Other Store (Non-Food)', 'Other
#    Animal' or 'unknown odor/taste in drinking water (QA6)' name a subject and stay in the pool.
GENERIC_DESCRIPTORS = [
    "Other", "N/A", "Other (complaint details)", "Other/Unknown", "Other - Explain Below",
    "General Complaint", "General Inquiry", "Unknown", "Unspecified", "None",
]

# i, k: what "belongs to" means, measured on full-year counts.
ZIP_MAJORITY_SHARE = 0.95  # a ZIP belongs to a borough if >= 95 % of its rows are there
TEXT_STATUS_SHARE = 0.95  # a text is "open" / "closed" language if >= 95 % of its rows are
MIN_TEXT_ROWS = 20  # ... and it has at least this many rows
REOPENED_STATUSES = ["Open", "In Progress", "Assigned"]
# k: closed_with_open_text must put words on a Closed row that contradict "Closed". ">= 95 % open"
#    is a statistic, and a text can earn it while saying nothing about status; a detector that
#    judges meaning would then be charged a miss for being right. So an open text is only put in if
#    it passes two screens that need no evaluation data: it is also >= 95 % open on Jan-Aug rows
#    alone (>= MIN_TEXT_ROWS of them), and its words meet this criterion, written down before any
#    candidate text was read:
#      KEEP a text only if the text itself says the request is not finished: under review or
#      investigation, scheduled, assigned, the agency will visit / inspect / contact, a work order
#      with a future completion, waiting for a form or documents.
#      EXCLUDE texts that give no status information (instructions such as "call 311", pure
#      acknowledgements, referrals without a pending action) and ambiguous ones.
#    Jan-Aug 2023 has 29 such texts (8 agencies); 23 pass. The 6 excluded, by a distinctive part of
#    the text (lower case, runs of spaces collapsed):
NEUTRAL_OPEN_TEXTS: dict[str, str] = {
    "please call 311 for further information": "DEP: an instruction (call 311), no status information",
    "your service request has been submitted to the department of buildings": "DOB: an acknowledgement plus 'check back later'; names no pending action",
    "would be best addressed by the department of investigation": "DOB: a referral to another agency, no pending action",
    "has received and processed your complaint": "DOHMH: an acknowledgement ('processed' even reads as done) and general information",
    "performed work to make the site safe and passable": "DPR: work done; removing 'any debris remaining' is conditional - ambiguous",
    "has issued a notice to the driver": "TLC: an enforcement step already taken (a usual closing text); the hearing is conditional - ambiguous",
}
# k: open_with_closed_text is the mirror: a Closed row with a ">= 95 % closed" text is reopened and
#    loses its closed_date, so its own text is the only evidence and must say the work is finished.
#    The same two screens apply: the text is also >= 95 % closed on Jan-Aug rows alone (>=
#    MIN_TEXT_ROWS of them), and its words meet this criterion, written down before any candidate
#    text was read:
#      KEEP a text only if the text itself says the agency's handling is finished: action taken or
#      the condition fixed; an inspection or response completed with an outcome (violation / summons
#      issued, no violation found, condition not observed, responsible party gone, no access and the
#      complaint closed, determined no action is needed); or the complaint explicitly closed / no
#      further action.
#      EXCLUDE texts that give no status information (status pointers such as "status ... is
#      available on the website", instructions such as "call 311", pure acknowledgements,
#      information-only texts) and ambiguous ones, e.g. a referral, a duplicate of a request still
#      being handled, or a failed attempt / no access that does not say the complaint is closed
#      (exclusion is the safe direction).
#    Jan-Aug 2023 has 415 such texts (14 agencies); 239 pass, in all 14 agencies. The 176 excluded,
#    by a distinctive part of the text (lower case, runs of spaces collapsed). Each key matches one
#    text, except where no key can: three DPR texts differ only in spacing, and 'N/A' and DOT's two
#    'not within its jurisdiction.' wordings are the start of 8 longer excluded texts, which their
#    keys cover too:
NEUTRAL_CLOSED_TEXTS: dict[str, str] = {
    "has already mailed you a complaint form": "DCWP: waits for the complaint form; DCA will contact later - not finished",
    "dca will investigate your request": "DCWP: an acknowledgement; DCA will investigate - not finished",
    "is scheduling an inspection which will take place within 35 days": "DCWP: an inspection being scheduled - not finished",
    "(dcwp) provided information about your complaint": "DCWP: information only (see the Notes to Customer), no outcome",
    "dcwp will contact you directly to report its status": "DCWP: review finished but the outcome is reported elsewhere - no status information",
    "you should contact the department of sanitation (dsny)": "DCWP: a referral (to DSNY)",
    "should be referred to the department of sanitation": "DCWP: a referral (to DSNY)",
    "referred to the new york state education department's office of the professions": "DCWP: a referral (to the NYS Office of the Professions)",
    "you should contact the new york police department (nypd)": "DCWP: a referral (to NYPD)",
    "should be referred to the new york state office of the attorney general": "DCWP: a referral (to the NYS Attorney General)",
    "should be referred to the new york police department": "DCWP: a referral (to NYPD)",
    "you should contact the new york state office of the attorney general": "DCWP: a referral (to the NYS Attorney General)",
    "you should contact the department of health and mental hygiene (dohmh)": "DCWP: a referral (to DOHMH)",
    "should be referred to the department of buildings": "DCWP: a referral (to DOB)",
    "you should contact the department of buildings (dob)": "DCWP: a referral (to DOB)",
    "bureau of proprietary school supervision": "DCWP: a referral (to the NYS school supervision bureau)",
    "should be referred to the department of motor vehicles": "DCWP: a referral (to DMV)",
    "should be referred to the new york state dept. of agriculture & markets": "DCWP: a referral (to NYS Agriculture and Markets)",
    "should be referred to the department of health and mental hygiene": "DCWP: a referral (to DOHMH)",
    "you should contact the new york state education department's office of the professions": "DCWP: a referral (to the NYS Office of the Professions)",
    "you can pursue your complaint in the court system": "DCWP: a referral (to the courts)",
    "you should contact the new york state department of agriculture and markets": "DCWP: a referral (to NYS Agriculture and Markets)",
    "you should contact the department of motor vehicles": "DCWP: a referral (to DMV)",
    "you should contact the commission on human rights (cchr)": "DCWP: a referral (to the Commission on Human Rights)",
    "environmental protection determined that this complaint is a duplicate of a previously filed complaint": "DEP: a duplicate; the original complaint is still being addressed",
    "will keep the location under observation for idling commercial vehicles": "DEP: the location will be kept under observation - future action",
    "the status of this service request is currently not available online": "DEP: a status pointer (call 311), no status information",
    "the original complaint is being addressed. if the condition persists, please call 311 (or 212-639-9675 if calling from a non-new york city area code) to submit a new complaint": "DEP: a duplicate ('previously reported by another customer'); the original is still being addressed",
    "the department of environmental protection scheduled an inspection": "DEP: an inspection scheduled - not finished",
    "found that debris was obstructing the catch basin": "DEP: a finding (debris in the catch basin), not said cleared - ambiguous",
    "a message was left for the complainant at the phone number provided.": "DEP: waiting to reach the complainant (message left) - not finished",
    "the letter was returned as undeliverable": "DEP: a failed attempt (letter undeliverable), not said closed",
    "inspected your complaint and referred it to the department of transportation": "DEP: a referral (to DOT)",
    "found that a spray cap was in use while the hydrant was running": "DEP: a finding (hydrant running with a spray cap), no outcome stated - ambiguous",
    "the original complaint is being addressed. if the condition persists, please call 311 (or 212-639-9675 if calling from a non-new york city area code) with more detailed information": "DEP: a duplicate ('previously reported by another customer'); the original is still being addressed",
    "determined that an inspection is warranted": "DEP: an inspection is warranted - future action",
    "sent a report to the complainant": "DEP: information only (a report was sent), no outcome stated",
    "but the phone number provided was incorrect": "DEP: a failed attempt (wrong phone number), not said closed",
    "referred it to another agency or utility for further action": "DEP: a referral (another agency or utility)",
    "please call 311 and ask for further assistance with the status": "DEP: a status pointer (call 311), no status information",
    "at the phone number provided but received no response": "DEP: a failed attempt (no response to a message), not said closed",
    "determined that this complaint is a duplicate to another service request": "DEP: a duplicate of another request, its state not given",
    "a defect was found done": "DEP: a garbled outcome ('a defect was found done') - ambiguous",
    "sent it to the bureau of environmental compliance for further action": "DEP: sent to another bureau for further action - not finished",
    "we did keep the area under observation": "DEP: the area was kept under observation, no outcome stated - ambiguous",
    "there was no answer at the phone number provided": "DEP: a failed attempt (no answer), not said closed",
    "the information provided by the complainant was incomplete or incorrect": "DEP: information incomplete or incorrect, no outcome or closure stated - ambiguous",
    "is attributed to a defective sewer": "DEP: a finding (defective sewer), not said repaired - ambiguous",
    "investigated this complaint but could not gain access to the location": "DEP: no access, not said closed",
    "a letter was sent after phone messages received no response": "DEP: a failed attempt (no response to calls, letter sent), not said closed",
    "is coordinating with their agency partners to address the condition": "DHS: still coordinating with partners - not finished",
    "has referred this request to the new york city police department": "DHS: a referral (to NYPD)",
    "reviewed your service request and determined that the issue is not within its jurisdiction": "DHS: not its jurisdiction, closure not stated - an implicit referral",
    "attempted to investigate this complaint twice but could not gain access": "DOB: no access (twice); 'file a new complaint' but not said closed",
    "were addressed under another service request number": "DOB: a duplicate addressed under another request - its state not clear",
    "attempted to investigate this complaint but could not locate the premises": "DOB: a failed attempt (premises not located), not said closed",
    "buildings reviewed this complaint and referred it to the department of transportation": "DOB: a referral (to DOT)",
    "referred it to the new york city housing authority": "DOB: a referral (to NYCHA)",
    "that resulted in no access. another attempt will be made": "DOB: no access; another attempt will be made - not finished",
    "buildings reviewed this complaint and referred it to the new york police department": "DOB: a referral (to NYPD)",
    "prepared a violation but were unable to issue it": "DOB: a violation prepared but not issued, next step not stated - ambiguous",
    "buildings reviewed this complaint and referred it to the department of environmental protection": "DOB: a referral (to DEP)",
    "referred it to the department of housing preservation and development": "DOB: a referral (to HPD)",
    "and was denied access. another attempt will be made": "DOB: denied access; another attempt will be made - not finished",
    "buildings reviewed this complaint and referred it to the department of sanitation": "DOB: a referral (to DSNY)",
    "buildings reviewed this complaint and referred it to the department of parks and recreation": "DOB: a referral (to Parks)",
    "buildings reviewed this complaint and referred it to the department of health and mental hygiene": "DOB: a referral (to DOHMH)",
    "your complaint has been referred.": "DOE: a referral, closure not stated",
    "forwarded it to the office of charter schools": "DOE: a referral (to the Office of Charter Schools, which will contact you)",
    "please contact the private school directly": "DOE: a referral (to the private school)",
    "found violations on the property you reported. follow-up inspections will be scheduled": "DOHMH: violations found, but follow-up inspections will be scheduled - not finished",
    "tried to inspect the property you reported but could not access the site": "DOHMH: no access, not said closed",
    "will review to see if a sanitary inspection is necessary": "DOHMH: another bureau will review whether to inspect - not finished",
    "the owner was issued a summons. follow-up inspections will be scheduled": "DOHMH: a summons, but follow-up inspections will be scheduled - not finished",
    "help track and control mosquito populations": "DOHMH: an acknowledgement (mosquito tracking), information only",
    "this service request was referred to the new york state agriculture and markets": "DOHMH: a referral (to NYS Agriculture and Markets)",
    "but the inspector could not gain access to the site": "DOHMH: no access, not said closed",
    "the owner is correcting the condition": "DOHMH: violations found, the owner is still correcting - ambiguous",
    "hygiene determined that this issue is not within its jurisdiction. it has been referred to the department of parks and recreation": "DOHMH: a referral (to Parks)",
    "could not locate the address provided": "DOHMH: a failed attempt (address not located), not said closed",
    "hygiene determined that this issue is not within its jurisdiction. it has been referred to the department of buildings": "DOHMH: a referral (to DOB)",
    'in the "notes to customer" field': "DOT: an information pointer (Notes to Customer), no outcome",
    "has requested the department of environmental protection address the issue": "DOT: a referral (to DEP), re-inspection in 60 days - not finished",
    "transportation determined that this complaint is a duplicate of a previously filed complaint": "DOT: a duplicate; the original complaint is still being addressed",
    "will inspect the complaint location and notify the property owner within 180 days": "DOT: will inspect within 180 days - future action",
    "inspected the condition and issued a corrective action repair": "DOT: a repair order, 30 days to correct, then a summons - not finished",
    "a sidewalk dismissal inspection (work done by owner) has been scheduled": "DOT: an inspection scheduled - not finished",
    "can be added to a curb contract (3-5 years)": "DOT: will inspect for a curb contract - future action",
    "the department of transportation has determined that this issue is not within its jurisdiction": "DOT: not its jurisdiction (alone, or referred to DSNY, Parks, the MTA, NYPD or DOB), closure not stated - a referral",
    "the establishment must remove all of the items immediately": "DOT: the establishment must remove the items - compliance pending, ambiguous",
    "has approved the sidewalk re-inspection request": "DOT: a re-inspection approved, date to come - future action",
    "inspected the condition and issued a summons to the responsible party": "DOT: a summons, but a re-inspection within 10 days - not finished",
    "the establishment has 24 hours to come into compliance": "DOT: 24 hours to comply - outcome pending, ambiguous",
    'in the "notes to customer."': "DOT: an information pointer (Notes to Customer), no outcome",
    "see customer notes for more information": "DOT: an information pointer (customer notes), no outcome",
    "will be planned for either the next or future resurfacing seasons": "DOT: resurfacing planned in 2-3 years - future action",
    "the department of transportation notified the appropriate responsible party of the complaint": "DOT: not its jurisdiction, responsible party notified - a referral",
    "inspected the condition you reported and determined that this issue is not within its jurisdiction": "DOT: not its jurisdiction, closure not stated - an implicit referral",
    "is expected to be repaired in 30 days": "DOT: repair expected in 30 days - not finished",
    "determined this street requires a reconstruction project": "DOT: a long-term reconstruction project - not finished",
    "it will be repaired within 30 days": "DOT: to be repaired within 30 days - not finished",
    "could not find the address you reported. if you would like to pursue the complaint, please check the address and call": "DOT: a failed attempt (address not found), not said closed",
    "inspected the condition you reported and issued a corrective action repair": "DOT: a repair order, 30 days to correct, then maybe a summons - not finished",
    "the next scheduled maintenance and cleaning will correct the condition": "DOT: the next scheduled maintenance will correct it - future action",
    "will be scheduled for this current resurfacing season": "DOT: resurfacing scheduled this season - future action",
    "has either notified or will notify the property owner": "DOT: 'has either notified or will notify' - possibly pending, ambiguous",
    "notified the new york state department of transportation of the complaint": "DOT: a referral (to NYS DOT)",
    "will be added to the next or future season's curb construction contract": "DOT: will inspect for a curb contract - future action",
    "asked the department of environmental protection to address the issue": "DOT: a referral (DEP asked to address it)",
    "it is expected to be repaired in a future project": "DOT: repair expected in a future project - not finished",
    "the department of transportation determined that this issue is not within its jurisdiction": "DOT: not its jurisdiction (alone, or referred to NYPD or DCA), closure not stated - a referral",
    "requires 30 days to respond to this type of complaint": "DOT: 30 days needed to respond - not finished",
    "inspected the condition you reported and issued a summons to the responsible party": "DOT: a summons, but a re-inspection within 10 days - not finished",
    "could not find the address you reported. if you would like to pursue the complaint, please check the address and submit": "DOT: a failed attempt (address not found), not said closed",
    "is shifting their street tree planting program to a neighborhood approach": "DPR: information only (tree planting program change)",
    "nyc parks created a work order for the tree condition": "DPR: a work order created; the work may take a year or more - not finished",
    "will be addressed in the next pruning cycle": "DPR: to be addressed in the next pruning cycle - future action",
    "the requested tree will be placed on an upcoming contract": "DPR: a tree to be placed on an upcoming contract - future action",
    "the timeframe for repair work depends on available funding": "DPR: meets the repair criteria, repair timing open - not finished",
    "does not perform tree work for phone or cable service": "DPR: a referral (to the phone or cable provider)",
    "is under review for a future capital project": "DPR: under review for a future capital project - not finished",
    "attempted to inspect the reported condition but could not find the location": "DPR: a failed attempt (location not found), not said closed",
    "needs to be handled by the department of transportation (dot)": "DPR: a referral (to DOT)",
    "inspected the condition and determined that the issue is out of their jurisdiction": "DPR: out of its jurisdiction, closure not stated - an implicit referral",
    "referred to parks enforcement patrol and will be monitored": "DPR: referred to Parks Enforcement Patrol, to be monitored - not finished",
    "your site has been waitlisted": "DPR: site waitlisted - not finished",
    "has been deferred because of seasonal considerations": "DPR: work deferred, to be corrected later - not finished",
    "nyc parks inspected and determined the issue was out of its jurisdiction": "DPR: out of its jurisdiction, may belong to another organization - an implicit referral",
    "informed the responsible public utility who will correct the condition": "DPR: a referral (the utility will correct it) - not finished",
    "determined that another entity needs to handle the condition": "DPR: a referral (another entity notified)",
    "needs to be handled by the department of sanitation (dsny)": "DPR: a referral (to DSNY)",
    "please contact the new york police department for assistance": "DPR: a referral (to NYPD)",
    "your request will be referred to the nypd for further action": "DSNY: a referral (a vehicle with plates, to NYPD)",
    "n/a": "DSNY: 'N/A' (also 'N/A; SR auto closes') - no resolution, no status information",
    "has forwarded the request to the nypd for further action": "DSNY: a referral (to NYPD)",
    "will address the situation at the location by providing educational outreach": "DSNY: educational outreach to come - future action",
    "location will be referred to the appropriate agency for further action": "DSNY: no DSNY violation, but referred for further action - ambiguous",
    "the department of sanitation found an open service request already exists": "DSNY: an open request already exists, closure not stated - a duplicate",
    "will note this location for periodic surveillance": "DSNY: location noted for future surveillance - future action",
    "observed illegal postings at the location": "DSNY: postings observed, a summons once the party is identified - not finished",
    "will only remove abandoned motor vehicles": "DSNY: information only (vehicle removal rules)",
    "was found to be unsubstantiated": "DSNY: 'not within its jurisdiction and/or unsubstantiated' - ambiguous",
    "the cleaning crew was not able to access the graffiti": "DSNY: no access to the graffiti, not said closed",
    "the cleaning crew could not locate the property": "DSNY: a failed attempt (property not located), not said closed",
    "inspected and partially cleaned the lot": "DSNY: partially cleaned, could not finish (ownership issues) - ambiguous",
    "the e-waste appointment could not be serviced": "DSNY: appointment not serviced (inaccessible), not said closed",
    "has noted this as a location of concern": "DSNY: location noted as of concern - no outcome",
    "the helicopter did not belong to a tour company": "EDC: not its jurisdiction, file with the FAA - a referral",
    "the original complaint is still open": "HPD: a duplicate; the original complaint is still open",
    "conducted or attempted to conduct an inspection": "HPD: a status pointer ('conducted or attempted', results online)",
    "hpd will attempt to contact you to schedule a follow-up inspection": "HPD: a follow-up lead inspection to be scheduled - not finished",
    "complaint number at 718 579-6777 (bronx)": "HPD: no access, not said closed (Bronx office)",
    "complaint number at 718 827 - 1955 (brooklyn)": "HPD: no access, not said closed (Brooklyn office)",
    "complaint number at 718 286 - 0823 (queens)": "HPD: no access, not said closed (Queens office)",
    "complaint number at 212 234 - 7485 (manhattan)": "HPD: no access, not said closed (Manhattan office)",
    "reviewed your complaint and provided additional information below": "NYPD: information only ('provided additional information below'), no outcome",
    "officers were unable to gain entry into the premises": "NYPD: no entry, not said closed",
    "does not fall under the police department's jurisdiction": "NYPD: not its jurisdiction, closure not stated - an implicit referral",
    "has referred the complaint to the department of homeless services (dhs) for further action": "NYPD: a referral (to DHS, which will inspect) - not finished",
    "additional information will be available later": "NYPD: an acknowledgement, information to come - not finished",
    "your complaint was referred to the vendor, citybridge": "OTI: a referral (to the vendor CityBridge)",
    "see notes for information": "TLC: an information pointer ('see notes'), no outcome",
    "a hearing was scheduled at the oath/taxi and limousine tribunal": "TLC: a hearing scheduled - not finished",
    "see notes for more information": "TLC: an information pointer ('see notes'), no outcome",
    "unable to identify the driver or car service company": "TLC: could not identify the driver - a failed attempt, not said closed",
    "unable to identify the driver or medallion owner": "TLC: could not identify the driver - a failed attempt, not said closed",
}
# k: both screens are hand judgments about the Jan-Aug candidates of this data snapshot, and a
#    re-download can bring a candidate nobody read. So InjectionContext.build warns unless the
#    candidates are the ones read: (how many, first 16 hex characters of the SHA-256 of their sorted
#    normalised texts, one per line). See _warn_if_unread.
READ_OPEN_CANDIDATES = (29, "a5fdacf69f111b43")
READ_CLOSED_CANDIDATES = (415, "ed2231cf673892a4")

# l: synthetic personal data. 555-0100..0199 numbers and example.* domains are reserved
#    for fiction; first and last names are combined at random.
FIRST_NAMES = [
    "Maria", "Anita", "James", "Wei", "Fatima", "Carlos", "Aisha", "Dmitri", "Keisha", "Rahul",
    "Sofia", "Kwame", "Mei", "Jose", "Olga", "Tyrone", "Priya", "Luis", "Hannah", "Omar",
    "Yuki", "Ana", "Malik", "Ingrid", "Diego", "Grace", "Samuel", "Leila", "Andre", "Nadia",
    "Victor", "Rosa", "Ethan", "Chloe", "Ibrahim", "Esther", "Marcus", "Lucia", "Patrick", "Jasmine",
]
LAST_NAMES = [
    "Gonzalez", "Desai", "Brennan", "Chen", "Rahman", "Rodriguez", "Johnson", "Kowalski", "Williams", "Patel",
    "Nguyen", "Mensah", "Wong", "Martinez", "Ivanova", "Brown", "Sharma", "Rivera", "Goldberg", "Haddad",
    "Tanaka", "Silva", "Jackson", "Larsen", "Lopez", "Kim", "Okafor", "Hosseini", "Baptiste", "Petrova",
    "Morales", "Cohen", "Murphy", "Dubois", "Ali", "Friedman", "Thompson", "Romano", "Sullivan", "Reyes",
]
AREA_CODES = ["212", "718", "347", "646", "917", "929"]
PHONE_FORMATS = ["({area}) 555-{line}", "{area}-555-{line}", "{area}.555.{line}", "{area}555{line}", "+1 {area} 555 {line}"]
EMAIL_FORMATS = ["{first}.{last}@example.com", "{initial}{last}@example.org", "{first}_{last}{n}@example.net", "{last}.{first}@example.com"]
PII_SENTENCES = {
    "phone": [
        "Tenant can be reached at {phone}.",
        "Please call back at {phone} to schedule access.",
        "Left a voicemail at {phone}.",
    ],
    "email": [
        "Follow-up sent to {email}.",
        "Photos of the condition were received from {email}.",
        "Updates requested at {email}.",
    ],
    "name_with_cue": [
        "Spoke with tenant {name}.",
        "Caller {name} reported the condition again.",
        "Complainant {name} was not home at the time of inspection.",
        "Inspector met resident {name} on site.",
        "Ms. {last} provided access to the apartment.",
    ],
    "name_bare": [
        "Per {name}, condition corrected.",
        "{name} confirmed the repair was completed.",
        "Access was arranged by {name}.",
        "Keys were left with {name}.",
    ],
    "mixed": [
        "Spoke with tenant {name} at {phone}.",
        "Contact {name} at {email} for access.",
        "{name} ({phone}) asked for a follow-up.",
        "Caller {name} can be reached at {phone} or {email}.",
    ],
}
# DESIGN: "appended sentence or replacing a clause" - replace = the sentence takes the place
# of one random sentence of the original text.
PII_PLACEMENTS = ["append", "replace"]

VARIANTS: dict[str, tuple[str, ...]] = {
    "a": ("rename", "drop"),
    "b": ("num_as_str", "num_as_str_partial", "zip_as_number"),
    "c": tuple(DATE_FORMATS),
    "d": ("swap",),
    "e": ("exact_copy",),
    "f": ("time_only", "time_and_text"),
    "g": ("null_spike",),
    "h": ("minutes", "days", "year_typo"),
    "i": ("zip_replaced", "borough_replaced"),
    "j": ("cross_agency", "same_agency"),
    "k": ("closed_with_open_text", "open_with_closed_text"),
    "l": tuple(PII_SENTENCES),
}


# --------------------------------------------------------------------------
# Contract (DESIGN.md section 3)
# --------------------------------------------------------------------------
@dataclass
class Injected:
    df: pd.DataFrame  # corrupted batch, positional index 0..len-1
    row_labels: np.ndarray  # bool[len(df)]: rows that carry the corruption (ground truth)
    batch_label: bool  # True if anything was injected
    problem: str  # 'a'..'l' ('clean' for as_clean)
    params: dict  # exactly what was done (variant, columns, agency, switch row, ...)
    row_variant: np.ndarray | None = None  # object[len(df)]: variant per injected row, None elsewhere


@dataclass
class InjectionContext:
    """Full-year lookups, built once from the UNsanitised feed. It keeps counts and small
    lookup tables only (a few hundred KB), never the 3.2 M-row frame."""

    zip_borough_counts: pd.DataFrame  # i: rows per 5-digit ZIP (index) x real borough (columns)
    descriptor_counts: pd.Series  # j: rows per descriptor (sampling weights; index = every descriptor but the catch-alls)
    type_uses: pd.DataFrame  # j: bool, complaint type x descriptor: pair seen in the full year (any spelling)
    agency_uses: pd.DataFrame  # j: bool, agency x descriptor: the agency uses the descriptor (any spelling)
    resolution_texts: pd.DataFrame  # k: frequent texts -> n, open_share, closed_share, agency, insertable_open, insertable_closed
    reference_null_rates: pd.DataFrame  # g: reference-period null rate, agency (index) x column

    @classmethod
    def build(cls, full_feed: pd.DataFrame) -> "InjectionContext":
        # Every lookup starts from a groupby that collapses millions of rows into a few
        # thousand distinct combinations; filtering happens on that small result.
        # dropna=False: a (complaint type, descriptor) pair seen only on rows without an
        # agency was still seen in 2023 (data/clean.py counts every row too).
        triples = full_feed.groupby(["agency", "complaint_type", "descriptor"], dropna=False).size().rename("n").reset_index()
        triples = triples[triples["descriptor"].notna()]  # "no descriptor" is never drawn as a replacement
        descriptor_counts = triples.groupby("descriptor")["n"].sum()
        catch_all = _spelling(descriptor_counts.index).isin(_spelling(pd.Index(GENERIC_DESCRIPTORS)))
        descriptor_counts = descriptor_counts[~catch_all]  # nor is 'Other' or 'N/A' (see GENERIC_DESCRIPTORS)
        return cls(
            zip_borough_counts=_zip_borough_counts(full_feed),
            descriptor_counts=descriptor_counts,
            type_uses=_uses_table(triples, "complaint_type", descriptor_counts.index),
            agency_uses=_uses_table(triples, "agency", descriptor_counts.index),
            resolution_texts=_resolution_texts(full_feed),  # also warns if k's screens meet unread candidates
            reference_null_rates=_reference_null_rates(full_feed),
        )


def inject(batch: pd.DataFrame, problem: str, rng: np.random.Generator, ctx: InjectionContext, rate: float = config.ROW_INJECTION_RATE, **params) -> Injected:
    """Corrupt one clean batch with `problem` ('a'..'l'). Optional `variant=` fixes the
    variant (else drawn uniformly); problem g also takes `fraction=`."""
    if problem not in INJECTORS:
        raise ValueError(f"unknown problem {problem!r}; expected one of {sorted(INJECTORS)}")
    return INJECTORS[problem](batch, rng, ctx, rate, **params)


def as_clean(batch: pd.DataFrame) -> Injected:
    """A batch without injection, in the same shape as an injected one (for false alarms)."""
    df = batch.reset_index(drop=True)
    return Injected(df=df, row_labels=np.zeros(len(df), dtype=bool), batch_label=False, problem="clean", params={})


# --------------------------------------------------------------------------
# Batches that are already corrupted (DESIGN section 7 mixes corruptions)
# --------------------------------------------------------------------------
def _has_contract_dtype(df: pd.DataFrame, column: str) -> bool:
    """Is `column` in the batch with its feed-contract type (text, float or integer)?"""
    if column not in df.columns:
        return False
    if config.FEED_DTYPES[column] == "str":  # pandas 'str', or object holding only strings
        return pd.api.types.infer_dtype(df[column], skipna=True) in ("string", "empty")
    return str(df[column].dtype) == config.FEED_DTYPES[column]


def _needs(problem: str, *columns: str):
    """Decorator for the row-level injectors, naming the columns each reads or writes. When the
    batch lacks one of them or holds it in another dtype (another corruption got there first),
    nothing is injected and params["unusable_columns"] says which - no exception, and no
    dropped column written back."""

    def decorate(injector: Callable[..., Injected]) -> Callable[..., Injected]:
        @functools.wraps(injector)
        def checked(batch, rng, ctx, rate, variant=None) -> Injected:
            unusable = [column for column in columns if not _has_contract_dtype(batch, column)]
            if not unusable:
                return injector(batch, rng, ctx, rate, variant=variant)
            if variant is not None:
                _checked(problem, variant)  # an unknown variant is still an error
            df = batch.reset_index(drop=True)
            none = np.array([], dtype=int)
            return _row_result(problem, df, none, none.astype(object), _row_count(rate, len(df)), unusable_columns=unusable)

        return checked

    return decorate


# --------------------------------------------------------------------------
# Injectors: function(batch, rng, ctx, rate, **params) -> Injected.
# Each starts from batch.reset_index(drop=True): a new frame with a fresh RangeIndex;
# Copy-on-Write copies a column the first time it is written, so the batch stays untouched.
# --------------------------------------------------------------------------
def rename_or_drop_column(batch, rng, ctx, rate, variant=None) -> Injected:
    """a: one random contract column renamed to a plausible alias, or dropped."""
    df = batch.reset_index(drop=True)
    variant = _pick_variant(rng, "a", variant)
    present = [column for column in config.COLUMNS if column in df.columns]  # another corruption may have taken one
    if not present:
        return _nothing_injected("a", df, variant=variant, columns=[], reason="no contract column left")
    column = _pick(rng, present)
    if variant == "drop":
        return _schema_result("a", df.drop(columns=column), {"variant": variant, "columns": [column], "dropped_column": column})
    display_name = column.replace("_", " ").title()  # "incident_zip" -> "Incident Zip"
    new_name = _pick(rng, COLUMN_ALIASES[column] + [display_name])
    params = {"variant": variant, "columns": [column], "old_name": column, "new_name": new_name}
    return _schema_result("a", df.rename(columns={column: new_name}), params)


def change_column_type(batch, rng, ctx, rate, variant=None) -> Injected:
    """b: a numeric column delivered as text (whole batch, or from a switch row on), or
    ZIP codes delivered as numbers. Only columns that still have their contract dtype qualify."""
    df = batch.reset_index(drop=True)
    numeric = [column for column in NUMERIC_COLUMNS if _has_contract_dtype(df, column)]
    possible = {"num_as_str": bool(numeric), "num_as_str_partial": bool(numeric), "zip_as_number": _has_contract_dtype(df, "incident_zip")}
    variant = _pick_variant(rng, "b", variant, possible)
    if not possible.get(variant):
        return _nothing_injected("b", df, variant=variant, columns=[], reason="no column left in the dtype this variant changes")
    if variant == "zip_as_number":
        # float, not int: a numeric column with nulls cannot be int64
        df["incident_zip"] = pd.to_numeric(df["incident_zip"], errors="coerce").astype("float64")
        return _schema_result("b", df, {"variant": variant, "columns": ["incident_zip"]})
    column = _pick(rng, numeric)
    params = {"variant": variant, "columns": [column]}
    if variant == "num_as_str":
        df[column] = df[column].astype("str")
    else:  # num_as_str_partial: numbers up to the switch, text after it -> one mixed object column
        params["switch_row"] = _switch_row(rng, len(df))
        values = df[column].astype(object)
        as_text = (np.arange(len(df)) >= params["switch_row"]) & values.notna().to_numpy()
        values.loc[as_text] = values.loc[as_text].map(str)
        df[column] = values
    return _schema_result("b", df, params)


def switch_date_format(batch, rng, ctx, rate, variant=None) -> Injected:
    """c: from a switch row on, the date columns arrive in another format. Only rows whose
    value actually changed are labelled (nulls stay null), and params["columns"] lists only
    the columns that changed (localisation is scored against it)."""
    df = batch.reset_index(drop=True)
    variant = _pick_variant(rng, "c", variant)
    switch_row = _switch_row(rng, len(df))
    after_switch = np.arange(len(df)) >= switch_row
    changed = np.zeros(len(df), dtype=bool)
    switched = []
    for column in SWITCHED_DATE_COLUMNS:
        if not _has_contract_dtype(df, column):  # dropped or renamed by another corruption
            continue
        stamps = _parse_dates(df[column])
        rows = after_switch & stamps.notna().to_numpy()
        new_text = stamps[rows].dt.strftime(DATE_FORMATS[variant])
        differs = (new_text != df.loc[rows, column]).to_numpy()
        if differs.any():
            df.loc[rows, column] = new_text
            changed[rows] |= differs
            switched.append(column)
    params = {
        "variant": variant,
        "format": DATE_FORMATS[variant],
        "switch_row": switch_row,
        "columns": switched,
        "n_requested": int(after_switch.sum()),
        "n_injected": int(changed.sum()),
    }
    return Injected(df=df, row_labels=changed, batch_label=bool(changed.any()), problem="c", params=params)


@_needs("d", "latitude", "longitude")
def swap_lat_lon(batch, rng, ctx, rate, variant=None) -> Injected:
    """d: latitude and longitude exchanged on k rows."""
    df = batch.reset_index(drop=True)
    # Only rows inside the NYC box: the swap then always lands outside it, and a row that
    # already arrived swapped is never swapped back and mislabelled.
    inside_nyc = df["latitude"].between(config.NYC_LAT_MIN, config.NYC_LAT_MAX) & df["longitude"].between(config.NYC_LON_MIN, config.NYC_LON_MAX)
    k = _row_count(rate, len(df))
    rows, variants = _choose_rows(rng, k, _restrict("d", variant, {"swap": inside_nyc.to_numpy()}))
    df.loc[rows, ["latitude", "longitude"]] = df.loc[rows, ["longitude", "latitude"]].to_numpy()
    return _row_result("d", df, rows, variants, k)


def insert_exact_duplicates(batch, rng, ctx, rate, variant=None) -> Injected:
    """e: exact copies of k rows, each inserted at a random position after its original."""
    df = batch.reset_index(drop=True)
    k = _row_count(rate, len(df))
    sources, variants = _choose_rows(rng, k, _restrict("e", variant, {"exact_copy": np.ones(len(df), dtype=bool)}))
    before = rng.integers(sources + 1, len(df) + 1)  # the copy goes in front of this row (len = at the end)
    return _with_copies("e", df, df.iloc[sources], sources, before, variants, k)


@_needs("f", "unique_key", "created_date", "closed_date", "agency", "complaint_type", "descriptor", "borough", "incident_address")
def insert_near_duplicates(batch, rng, ctx, rate, variant=None) -> Injected:
    """f: copies of k rows entered again 1-300 s later under a new unique_key; 'time_and_text'
    copies also get one small text edit, and a copy whose address got the typo is geocoded
    TYPO_SHIFT_M metres away (params['coord_shift_m']: copy position -> metres actually moved).
    Each copy sits at its time-ordered position."""
    df = batch.reset_index(drop=True)
    created = _parse_dates(df["created_date"])
    # A copy lands up to MAX_TIME_SHIFT_S after its original. A later, real report of the same
    # request less than NEAR_DUPLICATE_WINDOW_S after the copy would turn into an unlabelled
    # near-duplicate (of the copy), so originals with a report that close behind are not used.
    gap_to_next_report = created.groupby(_request_ids(df), sort=False).shift(-1) - created
    crowded = gap_to_next_report <= pd.Timedelta(seconds=MAX_TIME_SHIFT_S + NEAR_DUPLICATE_WINDOW_S)  # NaT: no next report
    has_time = (created.notna() & ~crowded).to_numpy()
    applicable = pd.DataFrame({edit: df[column].str.contains(pattern, na=False) for edit, (column, pattern) in TEXT_EDITS.items()})
    masks = {"time_only": has_time, "time_and_text": has_time & applicable.any(axis=1).to_numpy()}
    k = _row_count(rate, len(df))
    sources, variants = _choose_rows(rng, k, _restrict("f", variant, masks))
    kept, new_keys = _new_unique_keys(rng, df["unique_key"].to_numpy(), sources)
    sources, variants = sources[kept], variants[kept]

    shift = rng.integers(1, MAX_TIME_SHIFT_S + 1, size=len(sources)).astype("timedelta64[s]")
    new_created = created.to_numpy()[sources] + shift
    copies = df.iloc[sources].reset_index(drop=True)
    copies["unique_key"] = new_keys
    copies["created_date"] = _to_source_format(new_created)
    copies["closed_date"] = _to_source_format(_parse_dates(copies["closed_date"]).to_numpy() + shift)
    text_edits, typo_copies = _edit_copies(rng, copies, applicable.iloc[sources].reset_index(drop=True), variants)
    shift_m = _move_copies(rng, copies, typo_copies)  # row of `copies` -> metres moved

    # Batches arrive in created_date order (DESIGN section 1), so searchsorted finds the
    # copy's slot, never before its original; sorting the copies by time keeps two copies
    # that land in the same slot in time order too.
    before = np.maximum(np.searchsorted(created.to_numpy(), new_created, side="right"), sources + 1)
    order = np.argsort(new_created, kind="stable")
    out = _with_copies("f", df, copies.iloc[order], sources[order], before[order], variants[order], k, text_edits=text_edits)
    # copy_of lists the copies in the order they were passed (copies.iloc[order]), keyed by new position
    new_position = dict(zip(order.tolist(), out.params["copy_of"]))
    out.params["coord_shift_m"] = {new_position[i]: metres for i, metres in shift_m.items()}
    return out


def null_spike(batch, rng, ctx, rate, variant=None, fraction=None) -> Injected:
    """g: one agency loses a fraction of its values in a column it normally fills."""
    df = batch.reset_index(drop=True)
    variant = _pick_variant(rng, "g", variant)
    if fraction is not None and not 0 < fraction <= 1:
        raise ValueError(f"fraction must be in (0, 1], got {fraction}")
    if not _has_contract_dtype(df, "agency"):  # dropped or renamed by another corruption
        return _nothing_injected("g", df, variant=variant, n_requested=0, n_injected=0, reason="no agency column")
    options = _null_spike_options(df, ctx)
    sizes = df["agency"].value_counts().reindex(options.index)
    qualified = options.index[(sizes >= MIN_AGENCY_ROWS) & options.any(axis=1)]
    if qualified.empty:  # e.g. a tiny batch: report "nothing injected" rather than corrupt something else
        reason = f"no agency with >= {MIN_AGENCY_ROWS} rows and a well-filled candidate column"
        return _nothing_injected("g", df, variant=variant, n_requested=0, n_injected=0, reason=reason)

    agency = _draw(rng, sizes[qualified])  # probability proportional to the agency's rows
    columns = list(NULL_SPIKE_CHOICES[_pick(rng, np.flatnonzero(options.loc[agency].to_numpy()))])
    fraction = float(rng.uniform(*NULL_FRACTION_RANGE) if fraction is None else fraction)
    eligible = np.flatnonzero((df["agency"].eq(agency) & df[columns].notna().any(axis=1)).to_numpy())
    n_requested = max(1, round(fraction * len(eligible)))
    rows = np.sort(rng.choice(eligible, size=n_requested, replace=False))
    df.loc[rows, columns] = np.nan
    labels = np.zeros(len(df), dtype=bool)
    labels[rows] = True
    params = {
        "variant": variant,
        "agency": agency,
        "columns": columns,
        "fraction": fraction,
        "reference_null_rate": float(ctx.reference_null_rates.loc[agency, columns].max()),
        "n_eligible": len(eligible),
        "n_requested": n_requested,
        "n_injected": len(rows),
    }
    return Injected(df=df, row_labels=labels, batch_label=True, problem="g", params=params)


@_needs("h", "created_date", "closed_date")
def close_before_create(batch, rng, ctx, rate, variant=None) -> Injected:
    """h: closed_date set minutes or days before created_date, or its year mistyped."""
    df = batch.reset_index(drop=True)
    created = _parse_dates(df["created_date"])
    both_dates = (created.notna() & _parse_dates(df["closed_date"]).notna()).to_numpy()
    k = _row_count(rate, len(df))
    rows, variants = _choose_rows(rng, k, _restrict("h", variant, dict.fromkeys(VARIANTS["h"], both_dates)))
    for name, (low, high) in LAG_RANGES_S.items():
        these = rows[variants == name]
        lag = rng.integers(low, high + 1, size=len(these)).astype("timedelta64[s]")
        df.loc[these, "closed_date"] = _to_source_format(created.loc[these] - lag)
    these = rows[variants == "year_typo"]
    typed = TYPO_YEAR + df.loc[these, "closed_date"].str[4:]
    df.loc[these, "closed_date"] = typed.str.replace(f"{TYPO_YEAR}-02-29", f"{TYPO_YEAR}-02-28", regex=False)
    return _row_result("h", df, rows, variants, k)


@_needs("i", "incident_zip", "borough")
def mismatch_zip_borough(batch, rng, ctx, rate, variant=None) -> Injected:
    """i: ZIP or borough replaced so that the (ZIP, borough) pair never occurs in 2023."""
    df = batch.reset_index(drop=True)
    has_pair = (df["incident_zip"].str.fullmatch(r"\d{5}", na=False) & df["borough"].isin(config.BOROUGHS)).to_numpy()
    replacements = {
        "zip_replaced": ("incident_zip", _candidates_per_row(ctx, _foreign_zips, df, ["borough"], has_pair)),
        "borough_replaced": ("borough", _candidates_per_row(ctx, _unseen_boroughs, df, ["incident_zip", "borough"], has_pair)),
    }
    return _replace_cells("i", df, rng, rate, variant, replacements)


@_needs("j", "agency", "complaint_type", "descriptor")
def mismatch_descriptor(batch, rng, ctx, rate, variant=None) -> Injected:
    """j: descriptor replaced by one never seen with the row's complaint type in 2023, in any
    spelling of either (see _spelling)."""
    df = batch.reset_index(drop=True)
    has_values = df[["agency", "complaint_type", "descriptor"]].notna().all(axis=1).to_numpy()
    key = ["agency", "complaint_type"]
    replacements = {
        "cross_agency": ("descriptor", _candidates_per_row(ctx, _other_agency_descriptors, df, key, has_values)),
        "same_agency": ("descriptor", _candidates_per_row(ctx, _same_agency_descriptors, df, key, has_values)),
    }
    return _replace_cells("j", df, rng, rate, variant, replacements)


@_needs("k", "agency", "status", "resolution_description", "closed_date")
def contradict_status(batch, rng, ctx, rate, variant=None) -> Injected:
    """k: a Closed row gets "still open" resolution language of its own agency, or a Closed row
    with "finished" language is reopened and loses its closed_date (so only the text tells the
    truth). params['text_agency']: row position -> main agency of the text put in."""
    df = batch.reset_index(drop=True)
    texts = ctx.resolution_texts
    open_texts = texts.index[texts["open_share"] >= TEXT_STATUS_SHARE]
    closed_texts = texts.index[texts["insertable_closed"]]  # closed on Jan-Aug rows too and says "finished" (NEUTRAL_CLOSED_TEXTS)
    is_closed = df["status"].isin(config.CLOSED_STATUSES)
    text = df["resolution_description"]
    new_texts = _candidates_per_row(ctx, _open_texts, df, ["agency"], (is_closed & ~text.isin(open_texts)).to_numpy())
    masks = {
        "closed_with_open_text": _has_candidates(new_texts),
        "open_with_closed_text": (is_closed & text.isin(closed_texts)).to_numpy(),
    }
    k = _row_count(rate, len(df))
    rows, variants = _choose_rows(rng, k, _restrict("k", variant, masks))
    text_agency = {}
    for pos, name in zip(rows.tolist(), variants):
        if name == "closed_with_open_text":
            df.at[pos, "resolution_description"] = _draw(rng, new_texts[pos])
            text_agency[pos] = texts.at[df.at[pos, "resolution_description"], "agency"]
        else:
            df.at[pos, "status"] = _pick(rng, REOPENED_STATUSES)
            df.at[pos, "closed_date"] = np.nan
    return _row_result("k", df, rows, variants, k, text_agency=text_agency)


@_needs("l", "resolution_description")
def insert_personal_data(batch, rng, ctx, rate, variant=None) -> Injected:
    """l: a sentence with synthetic personal data appended to resolution_description, or put in
    place of one of its sentences. params: the sentence and its placement per row."""
    df = batch.reset_index(drop=True)
    has_text = df["resolution_description"].notna().to_numpy()
    k = _row_count(rate, len(df))
    rows, variants = _choose_rows(rng, k, _restrict("l", variant, dict.fromkeys(VARIANTS["l"], has_text)))
    inserted, placements = {}, {}
    for pos, name in zip(rows.tolist(), variants):
        inserted[pos] = _pii_sentence(rng, name)
        placements[pos] = _pick(rng, PII_PLACEMENTS)
        df.at[pos, "resolution_description"] = _place_sentence(rng, df.at[pos, "resolution_description"], inserted[pos], placements[pos])
    return _row_result("l", df, rows, variants, k, inserted=inserted, placements=placements)


# --------------------------------------------------------------------------
# Context builders
# --------------------------------------------------------------------------
def _zip_borough_counts(feed: pd.DataFrame) -> pd.DataFrame:
    pairs = feed.groupby(["incident_zip", "borough"]).size().rename("n").reset_index()
    pairs = pairs[pairs["incident_zip"].str.fullmatch(r"\d{5}") & pairs["borough"].isin(config.BOROUGHS)]
    counts = pairs.pivot(index="incident_zip", columns="borough", values="n")
    return counts.reindex(columns=config.BOROUGHS).fillna(0).astype("int64")


def _uses_table(triples: pd.DataFrame, key: str, descriptors: pd.Index) -> pd.DataFrame:
    """Bool table key x descriptor: did `key` appear with the descriptor in the full year? Both
    are compared by _spelling, so 'Heat/Hot Water' counts as seen with 'ENTIRE BUILDING'
    because 'HEAT/HOT WATER' is. A row lookup is one array, so candidate sets are a couple of
    numpy ops per key."""
    seen = pd.crosstab(_spelling(triples[key]), _spelling(triples["descriptor"])) > 0
    keys = pd.Index(triples[key].dropna().unique()).sort_values()
    by_spelling = seen.reindex(index=_spelling(keys), columns=_spelling(descriptors), fill_value=False)
    return by_spelling.set_axis(keys, axis=0).set_axis(descriptors, axis=1)


def _spelling(values):
    """A value's letters and digits, upper-cased (Series or Index). Case and punctuation do not
    make another descriptor or complaint type: HPD back-filled its Sep/Oct 2023 requests in
    Title Case next to the upper-case originals ('Heat/Hot Water' / 'Entire Building'), and
    'DOOR FRAME' and 'DOOR/FRAME' are one thing. Swapping one spelling for another is no
    mismatch, so j never does it."""
    return values.str.upper().str.replace(r"[^A-Z0-9]", "", regex=True)


def _resolution_texts(feed: pd.DataFrame) -> pd.DataFrame:
    """Per resolution text with >= MIN_TEXT_ROWS rows: rows, share in the open / closed
    status groups, and the agency that uses it most. Shares are over ALL rows of the text (a
    null or other status counts in the denominator), exactly as clean.status_mix_by_text
    computes them, so a text called >= 95 % open here is a conflict for the sanitiser too.
    `insertable_open` (k): the text is >= 95 % open in the full year AND on Jan-Aug rows alone
    (>= MIN_TEXT_ROWS of them, so it could be screened without evaluation data), and its words
    are not status-neutral (NEUTRAL_OPEN_TEXTS). `insertable_closed`: the same for closed
    (NEUTRAL_CLOSED_TEXTS)."""
    is_reference = (feed["created_date"] < config.REFERENCE_END).rename("reference")
    counts = feed.groupby(["resolution_description", "agency", "status", is_reference], dropna=False).size().rename("n").reset_index()
    counts = counts[counts["resolution_description"].notna()]  # "no text" is not a text
    counts["n_open"] = counts["n"] * counts["status"].isin(config.OPEN_STATUSES)
    counts["n_closed"] = counts["n"] * counts["status"].isin(config.CLOSED_STATUSES)
    texts = counts.groupby("resolution_description")[["n", "n_open", "n_closed"]].sum()
    texts["open_share"] = texts["n_open"] / texts["n"]
    texts["closed_share"] = texts["n_closed"] / texts["n"]
    per_agency = counts.groupby(["resolution_description", "agency"])["n"].sum().reset_index()
    top_agency = per_agency.sort_values("n", kind="stable").drop_duplicates("resolution_description", keep="last")
    texts["agency"] = top_agency.set_index("resolution_description")["agency"]
    reference = counts[counts["reference"]].groupby("resolution_description")[["n", "n_open", "n_closed"]].sum().reindex(texts.index, fill_value=0)
    open_in_reference = (reference["n"] >= MIN_TEXT_ROWS) & (reference["n_open"] / reference["n"] >= TEXT_STATUS_SHARE)
    closed_in_reference = (reference["n"] >= MIN_TEXT_ROWS) & (reference["n_closed"] / reference["n"] >= TEXT_STATUS_SHARE)
    words = texts.index.str.lower().str.replace(r"\s+", " ", regex=True)  # the form NEUTRAL_OPEN/CLOSED_TEXTS are written in
    _warn_if_unread("open", words[open_in_reference.to_numpy()], READ_OPEN_CANDIDATES)  # warns unless these are the texts each screen read
    _warn_if_unread("closed", words[closed_in_reference.to_numpy()], READ_CLOSED_CANDIDATES)
    neutral = np.array([any(part in text for part in NEUTRAL_OPEN_TEXTS) for text in words], dtype=bool)
    neutral_closed = np.array([any(part in text for part in NEUTRAL_CLOSED_TEXTS) for text in words], dtype=bool)
    texts["insertable_open"] = (texts["open_share"] >= TEXT_STATUS_SHARE) & open_in_reference & ~neutral
    texts["insertable_closed"] = (texts["closed_share"] >= TEXT_STATUS_SHARE) & closed_in_reference & ~neutral_closed
    return texts.loc[texts["n"] >= MIN_TEXT_ROWS, ["n", "open_share", "closed_share", "agency", "insertable_open", "insertable_closed"]]


def _warn_if_unread(kind: str, candidates: pd.Index, read: tuple[int, str]) -> None:
    """k: warn (never raise) when a screen's Jan-Aug candidates, as normalised texts, are not the
    ones read for it: their count or fingerprint (the first 16 hex characters of the SHA-256 of the
    sorted texts, one per line) differs from READ_OPEN_CANDIDATES / READ_CLOSED_CANDIDATES."""
    found = (len(candidates), hashlib.sha256("\n".join(sorted(candidates)).encode("utf-8")).hexdigest()[:16])
    if found == read:
        return
    diff = found[0] - read[0]
    change = f"at least {diff} new" if diff > 0 else f"at least {-diff} missing" if diff < 0 else "at least one new and one missing"
    warnings.warn(
        f"k: {found[0]} Jan-Aug {kind}-text candidates, {read[0]} were read ({change}; fingerprint {found[1]}, read {read[1]}): "
        f"unread candidates are not screened - read them, then update NEUTRAL_{kind.upper()}_TEXTS and READ_{kind.upper()}_CANDIDATES"
    )


def _reference_null_rates(feed: pd.DataFrame) -> pd.DataFrame:
    """Null rate per agency and candidate column, on the reference period only."""
    is_reference = (feed["created_date"] < config.REFERENCE_END).to_numpy()
    columns = sorted({column for choice in NULL_SPIKE_CHOICES for column in choice})
    missing = feed[columns].isna()[is_reference]  # filter the cheap bool frame, not the strings
    return missing.groupby(feed["agency"][is_reference]).mean()


# --------------------------------------------------------------------------
# Candidate finders: find(ctx, *key) -> pd.Series of weights indexed by candidate value
# --------------------------------------------------------------------------
def _foreign_zips(ctx: InjectionContext, borough: str) -> pd.Series:
    """i: ZIPs that belong to another borough and never appear with `borough`."""
    counts = ctx.zip_borough_counts
    rows = counts.sum(axis=1)
    belongs_elsewhere = (counts.max(axis=1) >= ZIP_MAJORITY_SHARE * rows) & (counts[borough] == 0)
    return rows[belongs_elsewhere]


def _unseen_boroughs(ctx: InjectionContext, zip_code: str, borough: str) -> pd.Series:
    """i: the other boroughs that never appear with this ZIP (equal weights)."""
    boroughs = ctx.zip_borough_counts.columns
    seen = _lookup_row(ctx.zip_borough_counts, zip_code) > 0
    return pd.Series(1.0, index=boroughs[~seen & (boroughs != borough)])


def _other_agency_descriptors(ctx: InjectionContext, agency: str, complaint_type: str) -> pd.Series:
    """j/cross_agency: descriptors the row's agency never uses and its complaint type never has."""
    allowed = ~_lookup_row(ctx.agency_uses, agency) & ~_lookup_row(ctx.type_uses, complaint_type)
    return ctx.descriptor_counts[allowed]


def _same_agency_descriptors(ctx: InjectionContext, agency: str, complaint_type: str) -> pd.Series:
    """j/same_agency: descriptors of the agency's other complaint types, never seen with this one."""
    allowed = _lookup_row(ctx.agency_uses, agency) & ~_lookup_row(ctx.type_uses, complaint_type)
    return ctx.descriptor_counts[allowed]


def _open_texts(ctx: InjectionContext, agency: str) -> pd.Series:
    """k: "still open" texts whose main agency is the row's own (equal weights). Another agency's
    text (a Parks tree work order on an NYPD row) would be a giveaway, so an agency without open
    texts gets none and its rows are not eligible; equal weights keep one frequent template from
    making up most insertions. Only texts open on Jan-Aug rows too whose words say "not finished"
    (insertable_open, see NEUTRAL_OPEN_TEXTS)."""
    texts = ctx.resolution_texts
    own_open = texts["insertable_open"] & (texts["agency"] == agency)
    return pd.Series(1.0, index=texts.index[own_open])


def _null_spike_options(df: pd.DataFrame, ctx: InjectionContext) -> pd.DataFrame:
    """g: bool table agency x position in NULL_SPIKE_CHOICES: the agency normally fills the
    column(s) (reference null rate below the cap) and has values in every one of them in this
    batch. A column the batch lost or retyped (another corruption) has none."""
    candidates = ctx.reference_null_rates.columns
    usable = [column for column in candidates if _has_contract_dtype(df, column)]
    has_values = df[usable].notna().groupby(df["agency"]).any().reindex(columns=candidates, fill_value=False)
    rates = ctx.reference_null_rates.reindex(has_values.index)  # agency unseen in reference: NaN, never eligible
    return pd.DataFrame(
        {i: (rates[list(choice)].max(axis=1) < MAX_REFERENCE_NULL_RATE) & has_values[list(choice)].all(axis=1) for i, choice in enumerate(NULL_SPIKE_CHOICES)}
    )


# --------------------------------------------------------------------------
# Shared helpers
# --------------------------------------------------------------------------
def _row_count(rate: float, n_rows: int) -> int:
    """k = max(1, round(rate * n)); capped later by the number of eligible rows."""
    return max(1, round(rate * n_rows))


def _pick(rng: np.random.Generator, options):
    """Uniform choice that keeps Python types (rng.choice would return numpy scalars)."""
    return options[int(rng.integers(len(options)))]


def _draw(rng: np.random.Generator, weights: pd.Series):
    """One index label of `weights`, chosen with probability proportional to its weight."""
    p = weights.to_numpy(dtype=float)
    return weights.index[rng.choice(len(p), p=p / p.sum())]


def _checked(problem: str, variant: str) -> str:
    if variant not in VARIANTS[problem]:
        raise ValueError(f"problem {problem}: unknown variant {variant!r}; expected one of {VARIANTS[problem]}")
    return variant


def _pick_variant(rng: np.random.Generator, problem: str, variant: str | None, possible: dict[str, bool] | None = None) -> str | None:
    """Batch-level problems: the requested variant, else a uniform draw among the variants the
    batch allows (`possible`; a clean batch allows all). None when it allows none."""
    if variant is not None:
        return _checked(problem, variant)
    options = [name for name in VARIANTS[problem] if possible is None or possible[name]]
    return _pick(rng, options) if options else None


def _restrict(problem: str, variant: str | None, masks: dict[str, np.ndarray]) -> dict[str, np.ndarray]:
    """Row-level problems: eligible rows per variant, narrowed to the requested variant."""
    return masks if variant is None else {variant: masks[_checked(problem, variant)]}


def _choose_rows(rng: np.random.Generator, k: int, masks: dict[str, np.ndarray]) -> tuple[np.ndarray, np.ndarray]:
    """Up to k distinct rows, each with a variant. Every pick draws the variant uniformly among
    the variants that still have an eligible row, then one of that variant's rows. So the mix
    is uniform however the eligible sets overlap, and a variant that runs out of rows leaves
    its share to the others ("as many as possible"). Rows come back ascending."""
    names = list(masks)
    free = np.column_stack([masks[name] for name in names])  # row x variant: eligible, not picked yet
    rows_left = free.sum(axis=0)
    picks = []
    for _ in range(k):
        open_variants = np.flatnonzero(rows_left)
        if not open_variants.size:
            break
        v = _pick(rng, open_variants)
        row = _pick(rng, np.flatnonzero(free[:, v]))
        rows_left -= free[row]
        free[row] = False
        picks.append((row, names[v]))
    picks.sort()
    return np.array([row for row, _ in picks], dtype=int), np.array([name for _, name in picks], dtype=object)


def _candidates_per_row(ctx: InjectionContext, find: Callable, df: pd.DataFrame, key_columns: list[str], where: np.ndarray) -> list[pd.Series | None]:
    """Weighted replacement candidates for every row in `where` (None elsewhere). find(ctx, *key)
    runs once per distinct key: a batch has a few hundred keys, not 5,000."""
    found: dict[tuple, pd.Series] = {}
    candidates = []
    for key, wanted in zip(df[key_columns].itertuples(index=False, name=None), where):
        if wanted and key not in found:
            found[key] = find(ctx, *key)
        candidates.append(found[key] if wanted else None)
    return candidates


def _has_candidates(candidates: list[pd.Series | None]) -> np.ndarray:
    return np.array([c is not None and len(c) > 0 for c in candidates], dtype=bool)


def _lookup_row(table: pd.DataFrame, key: str) -> np.ndarray:
    """One row of a context table (counts or bool) as an array. A key the full year never
    saw was seen with nothing: all False, which also compares as zero counts."""
    if key in table.index:
        return table.loc[key].to_numpy()
    return np.zeros(table.shape[1], dtype=bool)


def _replace_cells(problem: str, df: pd.DataFrame, rng: np.random.Generator, rate: float, variant: str | None, replacements: dict) -> Injected:
    """i / j: each variant overwrites one column with a draw from the row's own candidates."""
    masks = {name: _has_candidates(candidates) for name, (_, candidates) in replacements.items()}
    k = _row_count(rate, len(df))
    rows, variants = _choose_rows(rng, k, _restrict(problem, variant, masks))
    for pos, name in zip(rows, variants):
        column, candidates = replacements[name]
        df.at[pos, column] = _draw(rng, candidates[pos])
    return _row_result(problem, df, rows, variants, k)


def _row_result(problem: str, df: pd.DataFrame, rows: np.ndarray, variants: np.ndarray, n_requested: int, **extra) -> Injected:
    labels = np.zeros(len(df), dtype=bool)
    labels[rows] = True
    row_variant = np.full(len(df), None, dtype=object)
    row_variant[rows] = variants
    params = {"variants": dict(sorted(Counter(variants.tolist()).items())), "n_requested": n_requested, "n_injected": len(rows), **extra}
    return Injected(df=df, row_labels=labels, batch_label=bool(len(rows)), problem=problem, params=params, row_variant=row_variant)


def _schema_result(problem: str, df: pd.DataFrame, params: dict) -> Injected:
    """a / b: the batch as a whole is corrupted; no single row is wrong."""
    return Injected(df=df, row_labels=np.zeros(len(df), dtype=bool), batch_label=True, problem=problem, params=params)


def _nothing_injected(problem: str, df: pd.DataFrame, **params) -> Injected:
    """a / b / g: the batch offers nothing to corrupt (params say why); batch_label False."""
    return Injected(df=df, row_labels=np.zeros(len(df), dtype=bool), batch_label=False, problem=problem, params=params)


def _with_copies(problem: str, df: pd.DataFrame, copies: pd.DataFrame, sources: np.ndarray, before: np.ndarray, variants: np.ndarray, n_requested: int, **extra) -> Injected:
    """e / f: insert copies[i] in front of original row before[i] (len(df) = at the end) and
    label the copies. params['copy_of'] maps each copy to its original (new positions)."""
    n = len(df)
    order = np.argsort(np.concatenate([np.arange(n), before - 0.5]), kind="stable")
    out = pd.concat([df, copies], ignore_index=True).take(order).reset_index(drop=True)
    new_position = np.argsort(order)  # inverse permutation: where each row of [df, copies] ended up
    copy_rows = new_position[n:]
    copy_of = dict(zip(copy_rows.tolist(), new_position[sources].tolist()))
    return _row_result(problem, out, copy_rows, variants, n_requested, copy_of=copy_of, **extra)


def _new_unique_keys(rng: np.random.Generator, batch_keys: np.ndarray, sources: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """f: original key + U(1, MAX_KEY_OFFSET), never a key of the batch or of an earlier copy.
    Returns (indices into sources that got a key, their keys). A source whose successors are
    all taken is skipped: impossible with real 311 keys (sparse), possible with toy data."""
    taken = set(batch_keys.tolist())
    kept, new_keys = [], []
    for i, key in enumerate(batch_keys[sources].tolist()):
        free = [key + offset for offset in range(1, MAX_KEY_OFFSET + 1) if key + offset not in taken]
        if free:
            new_keys.append(_pick(rng, free))
            taken.add(new_keys[-1])
            kept.append(i)
    return np.array(kept, dtype=int), np.array(new_keys, dtype="int64")


def _request_ids(df: pd.DataFrame) -> np.ndarray:
    """f: one id per request in DESIGN section 2's sense of "the same request": agency,
    complaint type and descriptor (any case: HPD's Title Case back-fill of Sep/Oct 2023 repeats
    upper-case requests, and data/clean.py ignores case too), borough and incident address
    (any case and spacing, ADDRESS_SPELLINGS equal). A missing value is part of the key."""
    address = df["incident_address"].str.upper().str.replace(r"\s+", " ", regex=True).str.strip()
    for word, short in ADDRESS_SPELLINGS.items():
        address = address.str.replace(rf"\b{word}\b", short, regex=True)
    key = pd.DataFrame(
        {
            "agency": df["agency"],
            "complaint_type": df["complaint_type"].str.upper(),
            "descriptor": df["descriptor"].str.upper(),
            "borough": df["borough"],
            "address": address,
        }
    )
    return key.groupby(list(key.columns), dropna=False, sort=False).ngroup().to_numpy()


def _edit_copies(rng: np.random.Generator, copies: pd.DataFrame, applicable: pd.DataFrame, variants: np.ndarray) -> tuple[dict[str, int], np.ndarray]:
    """f: one random applicable text edit per 'time_and_text' copy (edits `copies` in place).
    Returns the count per edit and the rows of `copies` whose address got the typo."""
    counts, typo_copies = Counter(), []
    for i in np.flatnonzero(variants == "time_and_text"):
        edit = _pick(rng, [name for name in TEXT_EDITS if applicable.at[i, name]])
        column = TEXT_EDITS[edit][0]
        copies.at[i, column] = _edit_text(rng, edit, copies.at[i, column])
        counts[edit] += 1
        if edit == "typo":
            typo_copies.append(i)
    return dict(sorted(counts.items())), np.array(typo_copies, dtype=int)


def _move_copies(rng: np.random.Generator, copies: pd.DataFrame, rows: np.ndarray) -> dict[int, float]:
    """f: move copies `rows` TYPO_SHIFT_M metres in a random direction (edits `copies` in place).
    State-plane x/y move first, rounded to whole feet as the source delivers them (fractional x/y
    would give the copy away). Latitude/longitude then follow the new x/y through the EPSG:2263
    inverse and keep the original's own residual (a few mm), so they stay the exact function of x/y
    they are in real rows; a copy without x/y moves them by the drawn step in degrees. A null
    coordinate pair stays null, and a pair another corruption dropped or retyped is left alone.
    Returns the metres actually moved (after the rounding) per row that had coordinates."""
    metres = rng.uniform(*TYPO_SHIFT_M, size=len(rows))
    bearing = rng.uniform(0, 2 * np.pi, size=len(rows))  # clockwise from north
    east, north = metres * np.sin(bearing), metres * np.cos(bearing)
    moved = np.zeros(len(rows), dtype=bool)
    x = y = new_x = new_y = np.full(len(rows), np.nan)  # stay NaN when the batch has no usable x/y
    if _has_contract_dtype(copies, "x_coordinate_state_plane") and _has_contract_dtype(copies, "y_coordinate_state_plane"):
        x, y = copies.loc[rows, "x_coordinate_state_plane"].to_numpy(), copies.loc[rows, "y_coordinate_state_plane"].to_numpy()
        ok = ~np.isnan(x) & ~np.isnan(y)
        new_x, new_y = np.round(x + east * US_FEET_PER_METRE), np.round(y + north * US_FEET_PER_METRE)
        copies.loc[rows[ok], "x_coordinate_state_plane"] = new_x[ok]
        copies.loc[rows[ok], "y_coordinate_state_plane"] = new_y[ok]
        metres[ok] = np.hypot(new_x - x, new_y - y)[ok] / US_FEET_PER_METRE  # the step the rounding left
        moved |= ok
    if _has_contract_dtype(copies, "latitude") and _has_contract_dtype(copies, "longitude"):
        lat, lon = copies.loc[rows, "latitude"].to_numpy(), copies.loc[rows, "longitude"].to_numpy()
        ok = ~np.isnan(lat) & ~np.isnan(lon)
        new_lat, new_lon = lat + north / METRES_PER_DEGREE, lon + east / (METRES_PER_DEGREE * np.cos(np.radians(lat)))  # no x/y
        from_xy = ok & ~np.isnan(x) & ~np.isnan(y)  # lat/lon = original + (inverse of new x/y - inverse of old x/y)
        (lat_to, lon_to), (lat_from, lon_from) = _state_plane_to_latlon(new_x[from_xy], new_y[from_xy]), _state_plane_to_latlon(x[from_xy], y[from_xy])
        new_lat[from_xy], new_lon[from_xy] = lat[from_xy] + (lat_to - lat_from), lon[from_xy] + (lon_to - lon_from)
        copies.loc[rows[ok], "latitude"] = new_lat[ok]
        copies.loc[rows[ok], "longitude"] = new_lon[ok]
        moved |= ok
    return dict(zip(rows[moved].tolist(), metres[moved].tolist()))


def _state_plane_to_latlon(x: np.ndarray, y: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """f: latitude and longitude (degrees) of EPSG:2263 state-plane x/y (US survey feet): the
    Lambert Conic Conformal (2SP) inverse of IOGP Guidance Note 7-2, constants above."""
    e = np.sqrt((2 - 1 / GRS80_INV_F) / GRS80_INV_F)  # eccentricity (e^2 = 2f - f^2)

    def m(phi):
        return np.cos(phi) / np.sqrt(1 - (e * np.sin(phi)) ** 2)

    def t(phi):
        return np.tan(np.pi / 4 - phi / 2) / ((1 - e * np.sin(phi)) / (1 + e * np.sin(phi))) ** (e / 2)

    phi_1, phi_2, phi_origin = np.radians([*LCC_STANDARD_PARALLELS, LCC_LAT_ORIGIN])
    n = (np.log(m(phi_1)) - np.log(m(phi_2))) / (np.log(t(phi_1)) - np.log(t(phi_2)))
    a_f = GRS80_A * m(phi_1) / (n * t(phi_1) ** n)  # a * F
    east = (x - LCC_FALSE_EASTING_FT) / US_FEET_PER_METRE  # E - E_F, metres
    north = a_f * t(phi_origin) ** n - y / US_FEET_PER_METRE  # r_F - (N - N_F), metres
    t_point = (np.hypot(east, north) / a_f) ** (1 / n)  # t' from r' (n > 0)
    phi = np.pi / 2 - 2 * np.arctan(t_point)
    for _ in range(6):  # latitude by fixed-point iteration: each step multiplies the error by at most about e^2 (0.007)
        phi = np.pi / 2 - 2 * np.arctan(t_point * ((1 - e * np.sin(phi)) / (1 + e * np.sin(phi))) ** (e / 2))
    return np.degrees(phi), LCC_LON_ORIGIN + np.degrees(np.arctan2(east, north) / n)


def _edit_text(rng: np.random.Generator, edit: str, text: str) -> str:
    if edit == "street_abbrev":
        return re.sub(r"\bSTREET\b", "ST", text, count=1)
    if edit == "avenue_abbrev":
        return re.sub(r"\bAVENUE\b", "AVE", text, count=1)
    if edit == "double_space":
        cut = _pick(rng, [m.start() for m in re.finditer(" ", text)])
        return text[:cut] + " " + text[cut:]
    if edit == "typo":
        pos = _pick(rng, [m.start() for m in re.finditer("[A-Za-z]", text)])
        return text[:pos] + _pick(rng, string.ascii_uppercase.replace(text[pos].upper(), "")) + text[pos + 1 :]
    return text.upper() if text != text.upper() else text.lower()  # descriptor_case


def _pii_sentence(rng: np.random.Generator, variant: str) -> str:
    first, last = _pick(rng, FIRST_NAMES), _pick(rng, LAST_NAMES)
    phone = _pick(rng, PHONE_FORMATS).format(area=_pick(rng, AREA_CODES), line=f"01{rng.integers(100):02d}")
    email = _pick(rng, EMAIL_FORMATS).format(first=first.lower(), last=last.lower(), initial=first[0].lower(), n=rng.integers(10, 100))
    return _pick(rng, PII_SENTENCES[variant]).format(name=f"{first} {last}", last=last, phone=phone, email=email)


def _place_sentence(rng: np.random.Generator, text: str, sentence: str, placement: str) -> str:
    """'append': the sentence goes at the end. 'replace': it takes the place of one random
    sentence of the text; the other sentences and the spacing between them stay as they were
    (a one-sentence text is replaced whole, like a resolution typed by hand)."""
    if placement == "append":
        return f"{text.rstrip()} {sentence}"
    breaks = list(re.finditer(r"(?<=[.!?])\s+", text))  # whitespace after a sentence end
    starts, ends = [0] + [m.end() for m in breaks], [m.start() for m in breaks] + [len(text)]
    spans = [(start, end) for start, end in zip(starts, ends) if end > start] or [(0, len(text))]  # a blank text is replaced whole
    start, end = _pick(rng, spans)
    return f"{text[:start]}{sentence}{text[end:]}"


def _switch_row(rng: np.random.Generator, n_rows: int) -> int:
    low, high = SWITCH_RANGE
    return int(rng.integers(round(low * n_rows), round(high * n_rows) + 1))


def _parse_dates(values: pd.Series) -> pd.Series:
    return pd.to_datetime(values, format=config.SOURCE_DATETIME_FORMAT, errors="coerce")


def _to_source_format(stamps) -> pd.Series:
    """Timestamps back to the feed's text, e.g. 2023-10-16T08:00:16.000 (NaT -> null)."""
    return pd.Series(stamps).dt.strftime("%Y-%m-%dT%H:%M:%S.%f").str[:-3]


INJECTORS: dict[str, Callable[..., Injected]] = {
    "a": rename_or_drop_column,
    "b": change_column_type,
    "c": switch_date_format,
    "d": swap_lat_lon,
    "e": insert_exact_duplicates,
    "f": insert_near_duplicates,
    "g": null_spike,
    "h": close_before_create,
    "i": mismatch_zip_borough,
    "j": mismatch_descriptor,
    "k": contradict_status,
    "l": insert_personal_data,
}
