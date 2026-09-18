"""Note templates for the simulated dataset.

Written to read like real showroom speech: code-mixed mid-sentence, numerals
carrying the meaning, brand names in English inside Hindi syntax, and a
reasonable share of notes that contain nothing worth extracting.

Every template is a format string over the slots in SLOTS.
"""
from __future__ import annotations

MODELS = ["Model X", "Model Y", "Model X Pro"]
RIVALS = ["Ather", "Ola", "TVS", "Bajaj", "Hero", "River", "Ultraviolette"]

# --- price ------------------------------------------------------------------
PRICE = [
    "Sir ko {model} pasand aayi thi but EMI {emi} bata diya toh bole {emi_want} se upar nahi ja sakte.",
    "{model} ke liye aaye the, EMI {emi} sun ke ruk gaye. Bole budget {emi_want} tak hi hai.",
    "Customer bola {model} acchi hai par on-road price {price} zyada lag raha hai.",
    "Down payment {dp} bola toh customer ka face utar gaya, bole itna ek saath nahi de sakte.",
    "{model} dikhaya, sab theek tha, bas down payment {dp} pe atak gaye.",
    "Exchange mein purani gaadi ka sirf {exch} de rahe hain bola toh naraz ho gaye.",
    "Purani Activa ka exchange value {exch} bataya, customer ko kam laga, bole doosri jagah {exch_other} mil raha hai.",
    "Finance rate ka issue tha, {rate} percent bola toh bole bank se kam mil jayega.",
    "Interest rate zyada lag raha hai unhe, {rate} percent par tayyar nahi the.",
    "{model} ka price theek laga par EMI ka option {emi} se kam nahi tha, isliye ruk gaye.",
]
# --- battery ----------------------------------------------------------------
BATTERY = [
    "Range ko lekar doubt tha, {range_km} km claim karte ho par actual mein kitna chalegi ye poocha.",
    "Customer bola {range_km} km toh AC off karke hoga, real mein kam hi milega.",
    "Charging station ghar ke paas hai ya nahi, ye unki sabse badi tension thi.",
    "Charging time {charge_hr} ghante sun ke bole itna wait kaun karega.",
    "Battery kitne saal chalegi aur warranty kya hai, detail mein poocha.",
    "Battery replace karne ka kharcha {batt_cost} bataya toh customer ghabra gaya.",
    "Battery mein aag lagne wali news dekhi thi unhone, safety ko lekar dar tha.",
    "Sir ne poocha agar battery kharab ho gayi warranty ke baad toh kitna lagega.",
]
# --- service and delivery ---------------------------------------------------
SERVICE = [
    "Service center kitni door hai poocha, {city} mein sirf ek hi hai sun ke thoda hesitate kiya.",
    "Service ka turnaround kitna hai poocha, bole doosri company wale same day karte hain.",
    "Spare parts ki availability ka poocha, bole abhi EV ka parts milna mushkil hai.",
    "Delivery {weeks} hafte lagegi bola toh bole itna wait nahi kar sakte.",
    "Blue colour stock mein nahi hai, customer ko wahi chahiye tha.",
]
# --- competitive ------------------------------------------------------------
COMPETITIVE = [
    "{rival} bhi dekh ke aaye hain, compare kar rahe hain.",
    "{rival} wale ne {price_other} bola tha, humara kitna padega poocha.",
    "Abhi {rival} chala rahe hain, upgrade karna chahte hain.",
    "{rival} zyada pasand aa rahi hai unhe, design ki wajah se.",
    "{rival} ne zyada range claim kiya hai bola, {range_other} km.",
    "{rival} aur humara dono dekh ke decide karenge bole.",
]
# --- positive ---------------------------------------------------------------
POSITIVE = [
    "{model} ka design bahut pasand aaya, build quality ki bhi tareef ki.",
    "Test ride li aur bahut khush the, pickup acha laga unhe.",
    "Brand pe trust hai bole, unke bhai ne bhi yahi li hai.",
    "Resale value ka poocha, bola acha hai toh khush hue.",
]
# --- outcomes ---------------------------------------------------------------
BOOKED = ["Booking kar di.", "Token amount de diya.", "Booking confirm ho gayi aaj hi."]
HOT = ["Kal phir aayenge bole.", "Family ko dikha ke aayenge.", "Weekend pe decide karenge."]
WARM = ["Sochenge bol ke gaye hain.", "Abhi decide nahi kiya.", "Compare karke batayenge."]
LOST = ["{rival} le li unhone.", "{rival} mein book kar diya bole."]

# Notes with nothing extractable. Real capture streams are full of these, and a
# dataset without them would flatter the system.
NOISE = [
    "Haan toh main nikal raha hoon ab, kal milte hain.",
    "Bhai kal subah 10 baje aa jaunga, chaabi le lena.",
    "Aaj bheed thi bahut, garmi bhi thi, theek se baat nahi ho paayi kisi se.",
    "Stock update chahiye tha, white colour kitne hain check kar lena.",
    "Wo file table pe rakhi hai, dekh lena.",
    "Lunch pe ja raha hoon, aadha ghanta lagega.",
]

SLOTS = {
    "emi": ["3,800", "4,200", "4,500", "4,800", "5,200"],
    "emi_want": ["3,200", "3,500", "4,000", "4,200"],
    "price": ["1.28 lakh", "1.35 lakh", "1.42 lakh", "1.55 lakh"],
    "price_other": ["1.15 lakh", "1.22 lakh", "1.30 lakh"],
    "dp": ["20,000", "25,000", "30,000", "35,000"],
    "exch": ["18,000", "22,000", "25,000"],
    "exch_other": ["28,000", "30,000", "32,000"],
    "rate": ["9.5", "10.5", "11", "12"],
    "range_km": ["120", "140", "150", "165"],
    "range_other": ["170", "180", "190"],
    "charge_hr": ["4", "5", "6"],
    "batt_cost": ["45,000", "52,000", "60,000"],
    "weeks": ["2", "3", "4"],
}
