"""Seed list of Indian cities (name, lat, lon), approximate to ~0.05 degrees.

That is plenty: Open-Meteo's grid is ~45 km. The list is only the *scheduled*
layer; any other place is handled by the on-demand lookup. Add or remove
freely. grid.dedupe_by_cell() drops cities that share a grid cell.
"""
from grid import dedupe_by_cell

_RAW = [
    # metros and large cities
    ("Delhi", 28.61, 77.21), ("Mumbai", 19.08, 72.88), ("Kolkata", 22.57, 88.36),
    ("Chennai", 13.08, 80.27), ("Bengaluru", 12.97, 77.59), ("Hyderabad", 17.39, 78.49),
    ("Ahmedabad", 23.02, 72.57), ("Pune", 18.52, 73.86), ("Surat", 21.17, 72.83),
    ("Jaipur", 26.91, 75.79), ("Lucknow", 26.85, 80.95), ("Kanpur", 26.45, 80.35),
    ("Nagpur", 21.15, 79.09), ("Indore", 22.72, 75.86), ("Bhopal", 23.26, 77.41),
    ("Patna", 25.59, 85.14), ("Vadodara", 22.31, 73.18), ("Visakhapatnam", 17.69, 83.22),
    ("Coimbatore", 11.02, 76.96), ("Kochi", 9.93, 76.27),
    # Delhi NCR
    ("Ghaziabad", 28.67, 77.45), ("Noida", 28.54, 77.39), ("Gurugram", 28.46, 77.03),
    ("Faridabad", 28.41, 77.31), ("Meerut", 28.98, 77.71), ("Rohtak", 28.89, 76.61),
    ("Panipat", 29.39, 76.97),
    # north
    ("Chandigarh", 30.73, 76.78), ("Ludhiana", 30.90, 75.86), ("Amritsar", 31.63, 74.87),
    ("Jalandhar", 31.33, 75.58), ("Patiala", 30.34, 76.39), ("Bathinda", 30.21, 74.95),
    ("Pathankot", 32.27, 75.65), ("Ambala", 30.38, 76.78), ("Hisar", 29.15, 75.72),
    ("Dehradun", 30.32, 78.03), ("Haridwar", 29.95, 78.16), ("Haldwani", 29.22, 79.51),
    ("Shimla", 31.10, 77.17), ("Kullu", 31.96, 77.11), ("Srinagar", 34.08, 74.80),
    ("Jammu", 32.73, 74.87), ("Leh", 34.15, 77.58), ("Kargil", 34.56, 76.13),
    # Uttar Pradesh
    ("Varanasi", 25.32, 83.01), ("Prayagraj", 25.44, 81.85), ("Agra", 27.18, 78.01),
    ("Gorakhpur", 26.76, 83.37), ("Bareilly", 28.37, 79.43), ("Aligarh", 27.88, 78.08),
    ("Moradabad", 28.84, 78.78), ("Jhansi", 25.45, 78.57), ("Mathura", 27.49, 77.67),
    ("Saharanpur", 29.97, 77.55), ("Ayodhya", 26.80, 82.20), ("Raebareli", 26.23, 81.23),
    # Rajasthan / Gujarat / MP / Chhattisgarh
    ("Jodhpur", 26.24, 73.02), ("Udaipur", 24.59, 73.71), ("Kota", 25.18, 75.84),
    ("Bikaner", 28.02, 73.31), ("Ajmer", 26.45, 74.64), ("Jaisalmer", 26.92, 70.90),
    ("Sikar", 27.61, 75.14), ("Alwar", 27.55, 76.61), ("Sri Ganganagar", 29.92, 73.88),
    ("Barmer", 25.75, 71.40), ("Rajkot", 22.30, 70.80), ("Bhuj", 23.25, 69.67),
    ("Gwalior", 26.22, 78.18), ("Jabalpur", 23.18, 79.95), ("Ujjain", 23.18, 75.78),
    ("Sagar", 23.84, 78.74), ("Rewa", 24.53, 81.30), ("Satna", 24.60, 80.83),
    ("Ratlam", 23.33, 75.04), ("Raipur", 21.25, 81.63), ("Bilaspur", 22.08, 82.15),
    ("Bhilai", 21.21, 81.38), ("Korba", 22.36, 82.68), ("Jagdalpur", 19.07, 82.02),
    # east
    ("Ranchi", 23.34, 85.31), ("Jamshedpur", 22.80, 86.20), ("Dhanbad", 23.80, 86.43),
    ("Bokaro", 23.67, 86.15), ("Hazaribagh", 23.99, 85.36), ("Deoghar", 24.48, 86.70),
    ("Gaya", 24.80, 85.00), ("Muzaffarpur", 26.12, 85.39), ("Bhagalpur", 25.24, 86.97),
    ("Darbhanga", 26.15, 85.90), ("Purnia", 25.78, 87.47), ("Arrah", 25.56, 84.67),
    ("Bhubaneswar", 20.30, 85.82), ("Cuttack", 20.46, 85.88), ("Rourkela", 22.26, 84.85),
    ("Sambalpur", 21.47, 83.97), ("Berhampur", 19.31, 84.79), ("Puri", 19.81, 85.83),
    ("Asansol", 23.68, 86.98), ("Durgapur", 23.55, 87.32), ("Kharagpur", 22.31, 87.31),
    ("Siliguri", 26.73, 88.40), ("Malda", 25.01, 88.14), ("Bankura", 23.23, 87.07),
    ("Cooch Behar", 26.32, 89.45), ("Berhampore", 24.10, 88.25),
    # north-east
    ("Guwahati", 26.14, 91.74), ("Dibrugarh", 27.48, 94.91), ("Silchar", 24.83, 92.78),
    ("Jorhat", 26.75, 94.20), ("Tezpur", 26.63, 92.80), ("Nagaon", 26.35, 92.69),
    ("Gangtok", 27.33, 88.61), ("Itanagar", 27.08, 93.61), ("Kohima", 25.67, 94.11),
    ("Dimapur", 25.91, 93.73), ("Imphal", 24.82, 93.94), ("Aizawl", 23.73, 92.72),
    ("Shillong", 25.58, 91.89), ("Agartala", 23.83, 91.28),
    # Maharashtra / Goa
    ("Thane", 19.22, 72.98), ("Nashik", 19.99, 73.79), ("Aurangabad", 19.88, 75.34),
    ("Solapur", 17.66, 75.91), ("Kolhapur", 16.70, 74.24), ("Amravati", 20.93, 77.75),
    ("Nanded", 19.14, 77.32), ("Latur", 18.41, 76.56), ("Akola", 20.71, 77.00),
    ("Jalgaon", 21.00, 75.57), ("Sangli", 16.85, 74.57), ("Panaji", 15.50, 73.83),
    ("Margao", 15.27, 73.96), ("Daman", 20.40, 72.83), ("Silvassa", 20.27, 73.01),
    # south
    ("Mysuru", 12.30, 76.64), ("Mangaluru", 12.91, 74.86), ("Hubballi", 15.36, 75.12),
    ("Belagavi", 15.85, 74.50), ("Kalaburagi", 17.33, 76.83), ("Ballari", 15.14, 76.92),
    ("Vijayapura", 16.83, 75.71), ("Shivamogga", 13.93, 75.57), ("Davangere", 14.47, 75.92),
    ("Udupi", 13.34, 74.75), ("Hassan", 13.00, 76.10),
    ("Warangal", 17.97, 79.59), ("Karimnagar", 18.44, 79.13), ("Khammam", 17.25, 80.15),
    ("Vijayawada", 16.51, 80.65), ("Guntur", 16.31, 80.44), ("Tirupati", 13.63, 79.42),
    ("Kurnool", 15.83, 78.04), ("Anantapur", 14.68, 77.60), ("Nellore", 14.44, 79.99),
    ("Kakinada", 16.99, 82.25), ("Rajahmundry", 17.00, 81.80),
    ("Madurai", 9.93, 78.12), ("Tiruchirappalli", 10.79, 78.70), ("Salem", 11.66, 78.15),
    ("Vellore", 12.92, 79.13), ("Thanjavur", 10.79, 79.14), ("Tirunelveli", 8.73, 77.70),
    ("Thoothukudi", 8.76, 78.13), ("Nagercoil", 8.18, 77.43), ("Erode", 11.34, 77.72),
    ("Puducherry", 11.93, 79.83),
    ("Thiruvananthapuram", 8.52, 76.94), ("Kozhikode", 11.26, 75.78),
    ("Thrissur", 10.53, 76.21), ("Kollam", 8.89, 76.61), ("Kannur", 11.87, 75.37),
    ("Palakkad", 10.78, 76.65),
    # islands
    ("Port Blair", 11.62, 92.73), ("Kavaratti", 10.57, 72.64),
]

CITIES = [{"name": n, "lat": la, "lon": lo} for n, la, lo in _RAW]
POINTS = dedupe_by_cell(CITIES)   # one entry per grid cell, with "cell" id
