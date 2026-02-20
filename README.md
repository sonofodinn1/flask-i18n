## Usage

```py
from flask import Flask
from flask_i18n import Translations

translations = Translations()

def create_app():
    app = Flask(__name__)

    translations.init_app(app)

    return app