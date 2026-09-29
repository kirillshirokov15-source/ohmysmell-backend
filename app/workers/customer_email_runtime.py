"""Customer wholesale intake only; old email_runtime entrypoint is its alias."""
from app.workers.email_runtime import main

if __name__ == '__main__':
    main('customer')
