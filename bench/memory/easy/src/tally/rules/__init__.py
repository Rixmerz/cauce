from tally.rules.clearance import Clearance
from tally.rules.staff import Staff

RULES = {rule.name: rule for rule in (Clearance(), Staff())}
