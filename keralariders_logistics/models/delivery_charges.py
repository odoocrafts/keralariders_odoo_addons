from odoo import api, fields, models, _
from odoo.exceptions import ValidationError
import math

class DeliveryPackage(models.Model):
    _name = "logistics.delivery.package"
    _description = "Delivery Package"
    
    name = fields.Char(string="Name", required=True)
    is_default = fields.Boolean(string="Is Default", default=False)
    charge_ids = fields.One2many("logistics.delivery.charges", "package_id", string="Charge Slabs")
    
    @api.constrains("is_default")
    def _check_default_package(self):
        for rec in self:
            if rec.is_default:
                default_packages = self.search([("is_default", "=", True), ("id", "!=", rec.id)])
                if default_packages:
                    raise ValidationError(_("Only one package can be set as default!"))


class DeliveryCharges(models.Model):
    _name = "logistics.delivery.charges"
    _description = "Delivery Charge Slab"
    _order = "package_id, minimum_weight"

    name = fields.Char(
        string="Name",
        compute="_compute_name",
        store=True,
    )
    package_id = fields.Many2one("logistics.delivery.package", string="Delivery Package", ondelete="cascade")

    @api.depends("minimum_weight", "maximum_weight")
    def _compute_name(self):
        for rec in self:
            min_g = int(rec.minimum_weight * 1000)
            max_g = int(rec.maximum_weight * 1000)

            if min_g == 0:
                rec.name = f"1 g - {max_g} g"
            else:
                rec.name = f"{min_g + 1} g - {max_g} g"
                
    minimum_weight = fields.Float(
        string="Minimum Weight (kg)",
        digits=(16, 3),
        required=True,
    )
    maximum_weight = fields.Float(
        string="Maximum Weight (kg)",
        digits=(16, 3),
        required=True,
    )

    same_district_amount = fields.Monetary(
        string="Same District",
        required=True,
    )
    different_district_amount = fields.Monetary(
        string="Different District",
        required=True,
    )

    currency_id = fields.Many2one(
        "res.currency",
        required=True,
        default=lambda self: self.env.company.currency_id.id,
    )

    @api.constrains(
        "minimum_weight",
        "maximum_weight",
        "same_district_amount",
        "different_district_amount",
    )
    def _check_values(self):
        for rec in self:
            if rec.minimum_weight < 0:
                raise ValidationError(_("Minimum weight cannot be negative."))

            if rec.maximum_weight <= rec.minimum_weight:
                raise ValidationError(
                    _("Maximum weight must be greater than minimum weight.")
                )

            if rec.same_district_amount < 0:
                raise ValidationError(
                    _("Same district amount cannot be negative.")
                )

            if rec.different_district_amount < 0:
                raise ValidationError(
                    _("Different district amount cannot be negative.")
                )

    @api.constrains("minimum_weight", "maximum_weight", "package_id")
    def _check_overlapping_slabs(self):
        for rec in self:
            overlap = self.search([
                ("id", "!=", rec.id),
                ("package_id", "=", rec.package_id.id),
                ("minimum_weight", "<", rec.maximum_weight),
                ("maximum_weight", ">", rec.minimum_weight),
            ], limit=1)

            if overlap:
                raise ValidationError(
                    _(
                        "Weight slabs cannot overlap within the same package.\n\n"
                        "%s - %s kg overlaps with %s - %s kg."
                    )
                    % (
                        rec.minimum_weight,
                        rec.maximum_weight,
                        overlap.minimum_weight,
                        overlap.maximum_weight,
                    )
                )

    @api.model
    def calculate_delivery_charge(
        self,
        weight,
        same_district=False,
        gst_percent=18.0,
        additional_charge=25.0,
        additional_slab=0.5,
        package_id=None
    ):
        """
        Calculate delivery charge.
        """
        if weight <= 0:
            return 0.0

        if package_id:
            slabs = self.search([("package_id", "=", package_id)], order="minimum_weight")
        else:
            default_package = self.env["logistics.delivery.package"].search([("is_default", "=", True)], limit=1)
            if not default_package:
                raise ValidationError(_("No default delivery package configured."))
            slabs = self.search([("package_id", "=", default_package.id)], order="minimum_weight")

        if not slabs:
            raise ValidationError(_("No delivery charge slabs configured for this package."))

        # Find matching slab
        for slab in slabs:
            if slab.minimum_weight < weight <= slab.maximum_weight:
                amount = (
                    slab.same_district_amount
                    if same_district
                    else slab.different_district_amount
                )

                return round(amount, 2)

        # Above highest slab
        last_slab = slabs[-1]

        base_amount = (
            last_slab.same_district_amount
            if same_district
            else last_slab.different_district_amount
        )

        additional_weight = weight - last_slab.maximum_weight

        additional_units = math.ceil(
            additional_weight / additional_slab
        )

        amount = base_amount + (additional_units * additional_charge)

        return round(amount, 2)