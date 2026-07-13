"""Local API runner for development: python -m src.dev_runner"""

import uvicorn


def main() -> None:
    uvicorn.run("src.main:app", host="0.0.0.0", port=8020, reload=True)


if __name__ == "__main__":
    main()
