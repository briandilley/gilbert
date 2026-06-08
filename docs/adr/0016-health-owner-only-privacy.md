# Health data is owner-only; cross-user reads require a separate `health-admin` role

Health metrics are PHI-adjacent, so the privacy posture is stronger than Gilbert's usual
admin-sees-all default: **only the owner** can read their metrics, and even the built-in `admin`
role is excluded. Reading another user's health data requires the separately-seeded, level-0
`health-admin` role that is **never auto-granted** (not even to admins), and any such read is
audited, announced on the event bus, and notifies the target user.

The AI health tools never accept a `user_id` from the model — they read the injected `_user_id` or
raise — so the model can't be talked into reading someone else's metrics. The explicit
*admin-has-no-access* stance is the point, not an oversight.
