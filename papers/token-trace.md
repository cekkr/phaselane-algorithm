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
| 0 | `0xaa81443db40b5b1c43097327166e0e02` | P3 |
| 1 | `0x21faa3d7dacdd0e36103bdf69b2dbe77` | P0 |
| 2 | `0x888b21379781bc887f5d778c7b903179` | P2 |
| 3 | `0xa591e8bf0845bb6a46322befc003b4b4` | P1 |
| 4 | `0x5da9a61cd51d3ff367ba3113eb1d52ff` | P2 |
| 5 | `0x8abe0866002f7ce535808b65879d17b6` | P0 |
| 6 | `0x39d33ef184e9b1ddde964a83f06fd92e` | P1 |
| 7 | `0xe25bb134f354591bc4575918a9064674` | P3 |
| 8 | `0xc56aa5739df722c31e25cd016960b0fc` | P0 |
| 9 | `0x5c02f89f89b84cb8257ad888fa3d9957` | P1 |
| 10 | `0x3707398b4758b9a1737c9911ac1a9812` | P2 |
| 11 | `0x3a0fc3432c4dd5ff8a8446a3102bd2de` | P3 |
| 12 | `0xc4b3f3bae0d20b9750b43f66c88f642c` | P3 |
| 13 | `0xf968d10c70dbbb379d3e3508c2251622` | P2 |
| 14 | `0x615f4764ec38edccd78387d2203d56eb` | P1 |
| 15 | `0x93af54dd5d79da451932ab9b4df9451f` | P0 |

## Provider lane P0

| t | P0 token | match |
| --- | --- | --- |
| 0 | `0xeeafece1251ccf687691135f062cb4d7` |  |
| 1 | `0x21faa3d7dacdd0e36103bdf69b2dbe77` | match |
| 2 | `0x88ad5efb2c5761de52f141d23bc88540` |  |
| 3 | `0x816f62b524482e9f535a2554d1b201a4` |  |
| 4 | `0xfbcc12ba1996112e91f04f5e5752007b` |  |
| 5 | `0x8abe0866002f7ce535808b65879d17b6` | match |
| 6 | `0xaf724603668afe9e530ec505758015fd` |  |
| 7 | `0x69e26d5ca4b56e5ea99891eaaf15792c` |  |
| 8 | `0xc56aa5739df722c31e25cd016960b0fc` | match |
| 9 | `0xae80b360eb05a477cb2ac8ea2dd786bf` |  |
| 10 | `0x420b9e2d7ad7cdb5d0d4b487303f2f83` |  |
| 11 | `0x43fa9c9ceaf9dfb8484de6bd801cbd8f` |  |
| 12 | `0x1e85d55fdc37d0c649ac9f87f969b652` |  |
| 13 | `0xb0e25671a557959fa14f17691393990f` |  |
| 14 | `0xc87fefc64d406946a9e3faf2c82584d6` |  |
| 15 | `0x93af54dd5d79da451932ab9b4df9451f` | match |

## Provider lane P1

| t | P1 token | match |
| --- | --- | --- |
| 0 | `0xed74cbe26554bf9a270f1d1d90dcb25d` |  |
| 1 | `0xfcb1104a0c0ba7f9fa257abb65f4484f` |  |
| 2 | `0x673bce6b3a80330a1421b1bcf7102326` |  |
| 3 | `0xa591e8bf0845bb6a46322befc003b4b4` | match |
| 4 | `0xdfb35020bcc5eda2736a65a8660cd188` |  |
| 5 | `0xc64b06a0c2b808e627c3fe2910f0536d` |  |
| 6 | `0x39d33ef184e9b1ddde964a83f06fd92e` | match |
| 7 | `0x5c0d14d5cabd38ca17d323d54d8f0bcf` |  |
| 8 | `0x31e897180054b0ace446266558de4576` |  |
| 9 | `0x5c02f89f89b84cb8257ad888fa3d9957` | match |
| 10 | `0x05853bcdb9a0f096c698c1a8345420a1` |  |
| 11 | `0xd4565699201a3440b4cb70c15c48f3c8` |  |
| 12 | `0x3d49f02aaf8349cabff87adf656dddfb` |  |
| 13 | `0x298d005a6185ac15562addf92fa945c2` |  |
| 14 | `0x615f4764ec38edccd78387d2203d56eb` | match |
| 15 | `0x295062766449b339d2babc59ab90d7bc` |  |

## Provider lane P2

| t | P2 token | match |
| --- | --- | --- |
| 0 | `0xf84da16f0a9d23ca92b9598c2cccc4fb` |  |
| 1 | `0xb7f7f5fb372075bde5349158efa5fca9` |  |
| 2 | `0x888b21379781bc887f5d778c7b903179` | match |
| 3 | `0x8593eef83487f09b5b30612843e00397` |  |
| 4 | `0x5da9a61cd51d3ff367ba3113eb1d52ff` | match |
| 5 | `0x1715f8e9f51c0129907481780e0d03bc` |  |
| 6 | `0x9dd506512423c063f94c259c4517aff2` |  |
| 7 | `0xffa8817c6c4d6b3806705a3446de34ba` |  |
| 8 | `0x4d202a4ce2de455dd2f12d699a8e2b65` |  |
| 9 | `0xcb3d5d9c1fcbefebd131ee67788f7e5c` |  |
| 10 | `0x3707398b4758b9a1737c9911ac1a9812` | match |
| 11 | `0x5dd99772024b726362467dafd4a6ad5f` |  |
| 12 | `0x645bc475ddc85d3edd5fda65e100737f` |  |
| 13 | `0xf968d10c70dbbb379d3e3508c2251622` | match |
| 14 | `0xa9af89ad1e47092f6cb6e3ff59f49430` |  |
| 15 | `0xad5621c5d6a249f4fd6cc130133e8e4a` |  |

## Provider lane P3

| t | P3 token | match |
| --- | --- | --- |
| 0 | `0xaa81443db40b5b1c43097327166e0e02` | match |
| 1 | `0xcb440e82f41dd12c05d861a210208faa` |  |
| 2 | `0x39001496e37a7d61dcd0b67485376618` |  |
| 3 | `0xc204fbda03f97faa7bcf2a6b737960b3` |  |
| 4 | `0x0984a1b59d2493b60a85a6b186402bf0` |  |
| 5 | `0x7a42a0eae1727b8d0b41996c692ad5e3` |  |
| 6 | `0xa2a9e6284b90f8fa643db155f801e918` |  |
| 7 | `0xe25bb134f354591bc4575918a9064674` | match |
| 8 | `0x9dcc87ed4663e699f271be1c571433e1` |  |
| 9 | `0xa1aec92a45a9b8309c51ebeae3708f91` |  |
| 10 | `0xf7ddca66eb47f0744b70b1cecffef59f` |  |
| 11 | `0x3a0fc3432c4dd5ff8a8446a3102bd2de` | match |
| 12 | `0xc4b3f3bae0d20b9750b43f66c88f642c` | match |
| 13 | `0xb1dbfeaa9135401f55cb6c6aa769a19b` |  |
| 14 | `0xd3deef9dbdd25c405c3af02dd6784ee5` |  |
| 15 | `0x53ecfce7eeab443e8223131846a1cc4a` |  |

