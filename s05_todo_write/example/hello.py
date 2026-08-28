def greet(name: str) -> None:
    """Print a greeting message for the given name.

    Args:
        name: The person's name to greet.
    """
    message = "Hello, " + name
    print(message)


def main() -> None:
    """Run the main program logic."""
    greet("Claude")


if __name__ == "__main__":
    main()