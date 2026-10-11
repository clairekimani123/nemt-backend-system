from typing import Annotated

from pydantic import AfterValidator, EmailStr, StringConstraints

Email = Annotated[EmailStr, AfterValidator(lambda v: v.lower())]
# 12+ chars (NIST favours length over composition rules); argon2 handles the rest.
Password = Annotated[str, StringConstraints(min_length=12, max_length=128)]