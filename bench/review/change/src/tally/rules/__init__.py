from tally.rules.bulk import Bulk
from tally.rules.clearance import Clearance
from tally.rules.loyalty import Loyalty
from tally.rules.promo import Promo
from tally.rules.staff import Staff

RULES = {rule.name: rule for rule in (Clearance(), Promo(), Bulk(), Staff(), Loyalty())}
