import time
import signal
import random
import logging
import urllib.request
import json
from datetime import date, datetime, timedelta
from dataclasses import dataclass

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
    center: VaccinationCenter
    date: date
    clock_zone: ClockZone


centers: list[VaccinationCenter] = []
clock_zones: dict[int, ClockZone] = {}  # 1 -> "11:00-14:00", etc.

json_encode = lambda m: str.encode(json.dumps(m))


def send_telegram_message(msg) -> str:
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{telegram_token}/sendMessage",
        data=json_encode(
            {"chat_id": telegram_chat_id, "text": msg, "disable_notification": True}
        ),
        headers={"content-type": "application/json", "accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req) as f:
        res = json.loads(f.read().decode("utf-8"))
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
        for json_timeslot in res["timeslotsFree"]:
            yield Timeslot.from_json(json_timeslot)


def pretty_date(d):
    return format_date(d, "EEEE d MMMM", locale="el")


if __name__ == "__main__":
    centers, clock_zones = request_centers_and_clock_zones(zip_code, person_id)

    active_slots: dict[Slot, str] = {}

    def handler(signum, frame):
        logging.info("deleting messages for active slots")
        for msgid, _ in active_slots.items():
            delete_telegram_message(msgid)
        exit(0)

    signal.signal(signal.SIGINT, handler)

    siren = chr(0x1F6A8)
    green = chr(0x1F7E9)
    orange = chr(0x1F7E7)
    red = chr(0x1F7E5)
    book_url = "https://emvolio.gov.gr/app"
    book_now = f"Κλείστε τώρα! {book_url}"
    while True:
        processed = 0
        for center in centers:
            start_date = datetime.now().date() + timedelta(days=center.days_after_today)
            for _ in range(2):  # each iteration is one week worth of timeslots
                time.sleep(random.random())
                for ts in request_timeslots(person_id, center.id, start_date):
                    # our data is in date ascending order so start_date is ever increasing
                    start_date = ts.date + timedelta(days=1)
                    slot = Slot(center, ts.date, ts.clock_zone)
                    processed += 1
                    if ts.availability_percent > 0 and slot not in active_slots:
                        avail_emoji = orange
                        if ts.availability_percent < 100 / 3:
                            avail_emoji = red
                        elif ts.availability_percent > 200 / 3:
                            avail_emoji = green
                        msg = f"{siren}{siren} Διαθεσιμότητα {avail_emoji} για την %s (ώρα %s) στο %s! {siren}{siren}\n{book_now}" % (
                            pretty_date(ts.date),
                            ts.clock_zone,
                            center.name,
                        )
                        logging.info(msg)
                        msgid = send_telegram_message(msg)
                        active_slots[slot] = msgid
                    elif ts.availability_percent == 0 and slot in active_slots:
                        logging.info("filled: %s" % msg)
                        delete_telegram_message(active_slots[slot])
                        del active_slots[slot]

        logging.debug(f"processed {processed} slots")
        time.sleep(30 + 30 * random.random())
