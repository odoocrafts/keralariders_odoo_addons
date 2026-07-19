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

    # Define the base charges based on weight and district
    if weight <= 0.5:
        charge = 50
    elif weight <= 1:
        charge = 50 if same_district else 65
    elif weight <= 1.5:
        charge = 50 if same_district else 75
    elif weight <= 2:
        charge = 60 if same_district else 90
    elif weight <= 2.5:
        charge = 70 if same_district else 105
    elif weight <= 3:
        charge = 80 if same_district else 125
    else:
        # For weights above 3 kg, calculate additional charges
        additional_weight = weight - 3
        additional_charges = (additional_weight // 0.5) * 25
        charge = (80 if same_district else 125) + additional_charges

    return charge