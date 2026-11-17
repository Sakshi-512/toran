"""Give an owner a new private link and PIN: python -m scripts.new_access <slug>.

Team only, and only after the team has confirmed it is her: call or WhatsApp the phone number
on her live site (never a number she gives in the request), or, if her site has no number,
check with her through the contact form's email thread. The old link stops working at once,
she is signed out everywhere and remembered devices are revoked. The new link and PIN are
printed here once; give them to her directly and do not store them anywhere.
"""

import sys

from sqlmodel import Session, select

from app.config import get_settings
from app.db import get_engine
from app.models import Business
from app.services.auth import reissue_access
from app.services.sites import main_origin

CONFIRM = "confirmed"


def main(argv: list[str]) -> int:
    """Ask the team to confirm her identity, then reissue access."""
    if not argv:
        print("usage: python -m scripts.new_access <slug> [--confirmed]")
        return 2
    slug = argv[0]
    with Session(get_engine()) as session:
        business = session.exec(select(Business).where(Business.slug == slug)).first()
    if business is None or business.id is None or business.deleted_at is not None:
        print(f"No business with web address {slug}")
        return 1
    if business.is_sample:
        print("Sample businesses do not get new access.")
        return 1
    print(f"Business: {business.display_name} ({slug})")
    print("Before you continue, confirm it is her by calling or messaging the phone number")
    print("on her live site (not a number given in the request).")
    if "--confirmed" not in argv:
        answer = input(f'Type "{CONFIRM}" once you have confirmed it is her: ').strip().lower()
        if answer != CONFIRM:
            print("Stopped. Nothing changed.")
            return 1
    access = reissue_access(get_engine(), business.id)
    print("\nOld link and all sessions have stopped working.")
    print(f"New private link: {main_origin(get_settings())}/o/{access.link_token}")
    print(f"New PIN: {access.pin}")
    print("Give these to her directly. They are not stored anywhere.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
