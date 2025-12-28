# PCPL Token Trace (generated)

This file is auto-generated. Do not edit by hand.
Regenerate with: `python3 demo/export_token_trace.py --x 4 --token-bits 128 --seed 1337 --blocks 4`

Parameters:
- x = 4
- cycles = 16
- seed = 1337
- token_bits = 128

Provider matching order is defined per block by a permutation seeded from the block phase digest. The order is not round-robin and can repeat across block boundaries.

Permutation formula:

$$
\pi_B = Permute(perm_key, \Phi_{B \cdot x}), \quad idx_t = \pi_B[t \bmod x]
$$

## Block-level permutations

| block B | pi_B (slot order 0..x-1) | matching order |
| --- | --- | --- |
| 0 | [3, 0, 2, 1] | P3 -> P0 -> P2 -> P1 |
| 1 | [2, 0, 1, 3] | P2 -> P0 -> P1 -> P3 |
| 2 | [0, 1, 2, 3] | P0 -> P1 -> P2 -> P3 |
| 3 | [3, 2, 1, 0] | P3 -> P2 -> P1 -> P0 |

## Schedule (device-selected provider per cycle)

| t | block | slot | idx (device routes to) |
| --- | --- | --- | --- |
| 0 | 0 | 0 | 3 |
| 1 | 0 | 1 | 0 |
| 2 | 0 | 2 | 2 |
| 3 | 0 | 3 | 1 |
| 4 | 1 | 0 | 2 |
| 5 | 1 | 1 | 0 |
| 6 | 1 | 2 | 1 |
| 7 | 1 | 3 | 3 |
| 8 | 2 | 0 | 0 |
| 9 | 2 | 1 | 1 |
| 10 | 2 | 2 | 2 |
| 11 | 2 | 3 | 3 |
| 12 | 3 | 0 | 3 |
| 13 | 3 | 1 | 2 |
| 14 | 3 | 2 | 1 |
| 15 | 3 | 3 | 0 |

## Device tokens (verbatim)

| t | device token | matches provider |
| --- | --- | --- |
| 0 | `0x26a948bfe8744adc11eaf6581af41dad` | P3 |
| 1 | `0x836c0804ae7691c04f9d2d4c8e81b666` | P0 |
| 2 | `0xcef769d59b261f6241ee5ebeadd84a2d` | P2 |
| 3 | `0x2eadad9a993ea91790bb8d88c0dfc010` | P1 |
| 4 | `0x38dc632f8371dc3088c196be4f99ff75` | P2 |
| 5 | `0xf0f6cad1e40167de4c91541b8b1df52f` | P0 |
| 6 | `0x05e8f0c6699d789e756252ca0c8b22b4` | P1 |
| 7 | `0x082dd88477ec3e1371b84b9c3fa7b325` | P3 |
| 8 | `0xef6fba3df9b96843be9c9d8be84aa803` | P0 |
| 9 | `0xa41a32780d86f44293b54f32f9adf2ff` | P1 |
| 10 | `0x0b3951610e59ba3ea7dd9da4efe5a719` | P2 |
| 11 | `0xab503463aa5c1dd76ee86ac0f530c21c` | P3 |
| 12 | `0xe7a776729d6c1a78a90a39e4919d6cd6` | P3 |
| 13 | `0x073739610a8e36b5c7cd72ad08282b73` | P2 |
| 14 | `0x95e579907d48fe7c58c526f31abefc39` | P1 |
| 15 | `0xcd12b0e85bc096fc80ea802b3924b751` | P0 |

## Provider lane P0

| t | P0 token | match |
| --- | --- | --- |
| 0 | `0xc29c0fa323a5fe882a289611da4020ae` |  |
| 1 | `0x836c0804ae7691c04f9d2d4c8e81b666` | match |
| 2 | `0x8a7235a62729b131bb9e736f808e31c2` |  |
| 3 | `0xaee80105c4272be33ebe3c05b4fd9adf` |  |
| 4 | `0xab086b99464a6f83c4433df5a3373310` |  |
| 5 | `0xf0f6cad1e40167de4c91541b8b1df52f` | match |
| 6 | `0x665fb251c4849019d91317ce0e3983a9` |  |
| 7 | `0xdcd5d1512418db2ccd9088ea172e307b` |  |
| 8 | `0xef6fba3df9b96843be9c9d8be84aa803` | match |
| 9 | `0x92e422480933c50d69f9ad18c363b6b9` |  |
| 10 | `0x70e13909ab856e218d7a2e255ce1a6c8` |  |
| 11 | `0x9a66501cdf007b0187664ddbcd954226` |  |
| 12 | `0x0cf96ac9d00aadcbb60d991712eb6392` |  |
| 13 | `0xf973f687ec93806d0a9a752b821fb777` |  |
| 14 | `0x7f8d6923ae85ba945fdd79f39ce059b1` |  |
| 15 | `0xcd12b0e85bc096fc80ea802b3924b751` | match |

## Provider lane P1

| t | P1 token | match |
| --- | --- | --- |
| 0 | `0x01b83b97648c1c78aafe66f3b3e7c4b5` |  |
| 1 | `0xa5255e45521cfe81f1ef6313f328155c` |  |
| 2 | `0xd4c2acb4d90acfe62ad1ead5ef76e536` |  |
| 3 | `0x2eadad9a993ea91790bb8d88c0dfc010` | match |
| 4 | `0xe2ad20c11371675181d5a4df5b6401f6` |  |
| 5 | `0x8fad0ddb0e870c877005b9ca41740e3b` |  |
| 6 | `0x05e8f0c6699d789e756252ca0c8b22b4` | match |
| 7 | `0x4f269b5332813c2467b5afef3b1700b8` |  |
| 8 | `0x48c354a73990e79c7ccd39cbf1123183` |  |
| 9 | `0xa41a32780d86f44293b54f32f9adf2ff` | match |
| 10 | `0xc0524cf1266fdefff9c652e67c2dd4a1` |  |
| 11 | `0x4593dff57b3131b28e88d1d013666416` |  |
| 12 | `0xeedf53ea8cf944a71f3b3d7d17b70d13` |  |
| 13 | `0xc2422f24eac1c1b0c5b75b021295d981` |  |
| 14 | `0x95e579907d48fe7c58c526f31abefc39` | match |
| 15 | `0x4f512ed504dc8282a7de482240cee1d9` |  |

## Provider lane P2

| t | P2 token | match |
| --- | --- | --- |
| 0 | `0xbcc5ceef7ad9dd3a453a647266f58e8b` |  |
| 1 | `0x25f1ec79d68bad2619532a452865948b` |  |
| 2 | `0xcef769d59b261f6241ee5ebeadd84a2d` | match |
| 3 | `0xa132f160bdc16d7ff1c6e5e2da83b039` |  |
| 4 | `0x38dc632f8371dc3088c196be4f99ff75` | match |
| 5 | `0x5448bcdb4abf138787d4d2f4a3430bcf` |  |
| 6 | `0x7de687e1f02bbae86d2619bd1f173a42` |  |
| 7 | `0x23cfd4cc9a73636a5133bea067147316` |  |
| 8 | `0x36cc138cc36842553684611a53ee88bd` |  |
| 9 | `0x9153dd45c446faf735797d7fbf46bf00` |  |
| 10 | `0x0b3951610e59ba3ea7dd9da4efe5a719` | match |
| 11 | `0x6be422395bb50dc1e06a7d0710453e8c` |  |
| 12 | `0x188e93bce21b4b82eb5b971692289b64` |  |
| 13 | `0x073739610a8e36b5c7cd72ad08282b73` | match |
| 14 | `0x345d0e29369201b5c0bf3f232156e2c0` |  |
| 15 | `0x70eb74eceddb16a9321dca51e09dbf00` |  |

## Provider lane P3

| t | P3 token | match |
| --- | --- | --- |
| 0 | `0x26a948bfe8744adc11eaf6581af41dad` | match |
| 1 | `0x5435b8a411cce3d305d413f494c37247` |  |
| 2 | `0xca0fc50325422456b87e30820f96bdfe` |  |
| 3 | `0x5597ff795bc6e480ef2f6b7865bd3086` |  |
| 4 | `0xe58dceddf9a91dccebc34b1aea318d5b` |  |
| 5 | `0xe21e3a97c983211fc2b8503e6d01d5da` |  |
| 6 | `0x0fe6b016191fc319d3d04db997a2c16e` |  |
| 7 | `0x082dd88477ec3e1371b84b9c3fa7b325` | match |
| 8 | `0x47e2b829b3922acfc9f294240d171989` |  |
| 9 | `0x0250b1d1b2c1f1f682a52cee87a61791` |  |
| 10 | `0xd7e0c4a287ebba428d46583c4ad037e8` |  |
| 11 | `0xab503463aa5c1dd76ee86ac0f530c21c` | match |
| 12 | `0xe7a776729d6c1a78a90a39e4919d6cd6` | match |
| 13 | `0x8e9fbc708db74369097f14ea99786ab0` |  |
| 14 | `0xa589707d29a1e1a8741747e3ed2f7a83` |  |
| 15 | `0x93aeeba22230f0877ca0b20471abade3` |  |

