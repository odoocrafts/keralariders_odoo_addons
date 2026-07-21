import math

"""
Delivery charge slabs and rates:

1. 1 g to 500 g
	- Same district: 50
	- Outside district: 50

2. 500 g to 1 kg
	- Same district: 50
	- Outside district: 65

3. 1 kg to 1.5 kg
	- Same district: 50
	- Outside district: 75

4. 1.5 kg to 2 kg
	- Same district: 60
	- Outside district: 90

5. 2 kg to 2.5 kg
	- Same district: 70
	- Outside district: 105

6. 2.5 kg to 3 kg
	- Same district: 80
	- Outside district: 125

Additional: for each additional 500 g beyond 3 kg, add 25
GST: add 18% extra
"""

def calculate_delivery_charges(weight: float, same_district: bool) -> float:
    if weight <= 0:
        return 0

    charge = 0
    # Check if weight less than max base weight
    if weight <= 3:
        # Define the base charges based on weight and district
        if 0 < weight <= 0.5: # 1 g to 500 g
            charge = 40 if same_district else 55
        elif 0.5 < weight <= 1: # 500 g to 1 kg
            charge = 50 if same_district else 70
        elif 1 < weight <= 1.5: # 1 kg to 1.5 kg
            charge = 55 if same_district else 85
        elif 1.5 < weight <= 2: # 1.5 kg to 2 kg
            charge = 65 if same_district else 110
        elif 2 < weight <= 2.5: # 2 kg to 2.5 kg
            charge = 80 if same_district else 125
        elif 2.5 < weight <= 3: # 2.5 kg to 3 kg
            charge = 90 if same_district else 140
    else:
        # For weights above 3 kg, calculate additional charges
        base_charge = 90 if same_district else 140 # Max Base charge
        additional_weight = weight - 3

        # Charge ₹25 for every started 500 g above 3 kg
        additional_slabs = math.ceil((additional_weight) / 0.5)

        additional_charges = additional_slabs * 25 # Additional 25rs per for each 500gm
        charge = base_charge + additional_charges

    return charge