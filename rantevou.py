import time
import signal
import random
import logging
import urllib.request
import json
from datetime import date, datetime, timedelta
from dataclasses import dataclass
from threading import Lock

from babel.dates import format_date

logging.basicConfig(
    format="%(asctime)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
    level=logging.DEBUG,
)

config = json.loads(open("config.json").read())
headers = config["headers"]
person_id = int(config["person-id"])
zip_code = int(config["zip-code"])
telegram_config = config["telegram"]
telegram_token, telegram_chat_id = telegram_config["token"], telegram_config["chat-id"]


@dataclass(frozen=True)
class VaccinationCenter:
    id: int
    name: str
    days_after_today: int  # we can only query for availability days_after_today days after today

    @staticmethod
    def from_json(j):
        return VaccinationCenter(
            id=int(j["id"]),
            name=j["name"],
            days_after_today=int(j["daysAfterTodayToBook"]),
        )


@dataclass(frozen=True)
class ClockZone:
    id: int
    start: str
    end: str

    @staticmethod
    def from_json(j):
        return ClockZone(
            id=int(j["TIMEZONE_NUM"]),
            start=j["START_TIME"].strip(),
            end=j["END_TIME"].strip(),
        )

    def __repr__(self):
        return f"{self.start}-{self.end}"


@dataclass(frozen=True)
class Timeslot:
    date: date
    clock_zone: ClockZone
    availability_percent: int

    @staticmethod
    def from_json(j):
        return Timeslot(
            date=datetime.fromisoformat(j["onDate"]).date(),
            clock_zone=clock_zones[int(j["zoneNum"])],
            availability_percent=int(j["percentAvailable"]),
        )


@dataclass(frozen=True)
class Slot:
    center_id: int
    date: date
    clock_zone: ClockZone


centers: list[VaccinationCenter] = []
clock_zones: dict[int, ClockZone] = {}  # 1 -> "11:00-14:00", etc.

json_encode = lambda m: str.encode(json.dumps(m))


def warn_if_no_key(d, key):
    if key not in d:
        logging.warn(f"expected '{key}' in '{d}' but was not there")


def send_telegram_message(msg) -> str:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        data=json_encode(
            {"chat_id": telegram_chat_id, "text": msg, "disable_notification": False}
        ),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as f:
        res = json.loads(f.read().decode("utf-8"))
        warn_if_no_key(res, "result")
        return res["result"]["message_id"]


def delete_telegram_message(msgid):
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{telegram_token}/deleteMessage",
        data=json_encode({"chat_id": telegram_chat_id, "message_id": msgid}),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    return urllib.request.urlopen(req)


def request_centers_and_clock_zones(zip_code, person_id):
    req = urllib.request.Request(
        "https://emvolio.gov.gr/app/api/CovidService/CV_User_NearCenters",
        data=json_encode(
            {
                "zipCode": zip_code,
                "personId": person_id,
            }
        ),
        headers=headers,
    )
    with urllib.request.urlopen(req) as f:
        res = json.loads(f.read().decode("utf-8"))
        for k in ("centers", "timezones"):
            warn_if_no_key(res, k)
        centers = list(map(VaccinationCenter.from_json, res["centers"]))
        clock_zones = {cz.id: cz for cz in map(ClockZone.from_json, res["timezones"])}
        return (centers, clock_zones)


def request_timeslots(person_id, center_id, start_date):
    req = urllib.request.Request(
        "https://emvolio.gov.gr/app/api/CovidService/CV_TimeSlots_Free",
        data=json_encode(
            {
                "centerId": center_id,
                "personId": person_id,
                "firstDoseDate": None,
                "zoneNum": None,
                "selectedDate": "%d-%02d-%02dT00:00:00.000Z"
                % (
                    start_date.year,
                    start_date.month,
                    start_date.day,
                ),
                "dose": 1,
                "requestRecommended": True,
            }
        ),
        headers=headers,
    )
    with urllib.request.urlopen(req) as f:
        res = json.loads(f.read().decode("utf-8"))
        warn_if_no_key(res, "timeslotsFree")
        for json_timeslot in res.get("timeslotsFree", []):
            yield Timeslot.from_json(json_timeslot)


def pretty_date(d):
    return format_date(d, "EEEE d MMMM", locale="el")


def availability_emoji(percentage):
    emoji = yellow
    if percentage < 100 / 3:
        emoji = orange
    elif percentage > 200 / 3:
        emoji = green
    return emoji


siren = chr(0x1F6A8)
orange = chr(0x1F7E7)
yellow = chr(0x1F7E8)
green = chr(0x1F7E9)
book_url = "https://emvolio.gov.gr/app"
book_now = f"Κλείστε τώρα! {book_url}"


def format_message(ts: Timeslot, center: VaccinationCenter):
    box = availability_emoji(ts.availability_percent)
    return f"{siren}{siren} Διαθεσιμότητα {box} για {pretty_date(ts.date)} (ώρα {ts.clock_zone}) στο {center.name}! {siren}{siren}\n{book_now}"


if __name__ == "__main__":
    active_slots_mu = Lock()  # for avoiding race condition with signal handlers
    active_slots: dict[Slot, str] = {}

    def handler(signum, frame):
        active_slots_mu.acquire()
        logging.info("deleting messages for active slots")
        for _, msgid in active_slots.items():
            delete_telegram_message(msgid)
        exit(0)

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)

    while True:
        centers, clock_zones = request_centers_and_clock_zones(zip_code, person_id)
        processed = 0

        for center in centers:
            start_date = datetime.now().date() + timedelta(
                days=center.days_after_today + 1
            )
            for _ in range(2):  # each iteration is one week worth of timeslots
                time.sleep(0.5 * random.random())
                for ts in request_timeslots(person_id, center.id, start_date):
                    # our data is in date ascending order so start_date is ever increasing
                    start_date = ts.date + timedelta(days=1)
                    slot = Slot(center.id, ts.date, ts.clock_zone)
                    processed += 1
                    active_slots_mu.acquire()
                    if ts.availability_percent > 0 and slot not in active_slots:
                        msg = format_message(ts, center)
                        logging.info(f"new: {slot} with {msg}")
                        msgid = send_telegram_message(msg)
                        active_slots[slot] = msgid
                    elif ts.availability_percent == 0 and slot in active_slots:
                        logging.info(f"filled: {slot}")
                        delete_telegram_message(active_slots[slot])
                        del active_slots[slot]
                    active_slots_mu.release()

        logging.debug(f"processed {processed} slots")
        time.sleep(10 + 10 * random.random())
