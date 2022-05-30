import json
import random

alphabet = 'abcdefghijklmnopqrstuvwxyz01234567890'
name_dict = {}
name_num = 200

while len(name_dict) < name_num:
    s = ''.join(random.sample(alphabet, 6))
    name_dict[s] = 0

# print(name_dict)
with open('user_id.txt', 'w+') as f:
    json.dump(list(name_dict.keys()), f)