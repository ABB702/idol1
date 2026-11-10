import os

import uvicorn

from .config import Config


def main():
    config = Config()
    # Never print secrets; an unset token is deliberately inaccessible to arbitrary clients.
    os.environ["IDOL_TOKEN"] = config.token
    uvicorn.run("idol.main:app", host="127.0.0.1", port=config.port, access_log=False, log_level="warning")


if __name__ == "__main__":
    main()
