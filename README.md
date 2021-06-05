* Copy `config.sample.json` to `config.json` and replace the placeholder values. All leaf values should be strings.
* Create a Telegram bot and get a token for it for `telegram.token`. Obtain the chat id for the chat you want the bot to send notifications on for `telegram.chat-id`.
* `headers`, `person-id` can be obtained by observing the XHR requests made when visiting [the platform](https://emvolio.gov.gr/app). Fill in `zip-code`.
* Install dependencies: `python3 -m pip install -r requirements.txt`
* Run: `python3 rantevou.py`
