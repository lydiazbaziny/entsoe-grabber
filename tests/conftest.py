import os

# Set before any test module imports entsoe_grabber.handler, which builds its
# S3 client at import time the way AWS recommends. The devcontainer exports an
# empty AWS_PROFILE, which makes botocore raise ProfileNotFound; the dummy
# credentials keep moto from ever reaching a real account.
os.environ.pop("AWS_PROFILE", None)
os.environ["AWS_ACCESS_KEY_ID"] = "testing"
os.environ["AWS_SECRET_ACCESS_KEY"] = "testing"
os.environ["AWS_SESSION_TOKEN"] = "testing"
os.environ["AWS_DEFAULT_REGION"] = "eu-central-1"
os.environ["AWS_REGION"] = "eu-central-1"
