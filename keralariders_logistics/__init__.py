from . import models
from . import controllers
from . import wizard


def _post_init_assign_hub_pincodes(env):
    """Assign all district pincodes to their hubs after install/upgrade."""
    env['logistics.hub'].assign_all_hub_pincodes()
