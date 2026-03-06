"""twitter-digest: Fetch and digest Twitter/X content with LLM post-processing."""

import os

from dotenv import load_dotenv

load_dotenv()


def main():
    bearer_token = os.getenv("TWITTER_BEARER_TOKEN")
    if not bearer_token:
        print("Error: TWITTER_BEARER_TOKEN not set in .env")
        return

    # TODO: Fetch tweets
    # TODO: LLM digest step
    print("twitter-digest — not yet implemented")


if __name__ == "__main__":
    main()
