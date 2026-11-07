> Provisional screening, not an accepted quality verdict. Read README.md and root-disposition.json first: some flags have been rejected on direct source inspection. The original labels below are retained for audit only.

# Public-source Notes acceptance score

## Verdict

- Model quality: **PASS_WITH_PARTIALS** — 110 supported, 11 partial, 0 unsupported bullets out of 121; 4 certainty promotions; 0 invented owner/deadline fields; 0 exact duplicate pairs.
- Meeting acceptance: **NOT ESTABLISHED**. Public row snippets cannot prove a meeting, real duration, speaker count, owners/deadlines, or the original private acceptance criteria.
- Gold is a frozen historical aid, not blind or human-approved. It was read before generated results; source/gold tensions are recorded in scores.json.

## Verification

- Repo state: PASS. Input state and all plan input hashes: PASS.
- Output manifest before results audit: PASS, state `81faed193ed4ea5049752898e2b3023fd277b0b6d7680281df35a7f40ef7c1f9`.
- Output manifest after results audit: PASS, same state `81faed193ed4ea5049752898e2b3023fd277b0b6d7680281df35a7f40ef7c1f9`.
- All 121 cited-bullet records resolve to public source IDs. Citation sufficiency: 110 sufficient, 11 partial, 0 insufficient.

## Gold-fact coverage

| scope | found | partial | missing | total |
|---|---:|---:|---:|---:|
| four full cohorts | 100 | 8 | 0 | 108 |
| repeated VietMed (three cases) | 77 | 4 | 0 | 81 |
| controls: clear/noise | 4 | 1 | 0 | 5 |
| all scoped fact records | 156 | 11 | 0 | 167 |

| case | found | partial | missing | partial/missing fact refs |
|---|---:|---:|---:|---|
| librispeech-1 | 23 | 4 | 0 | 0/c1a143de58f362dd570d203dd7adbb0e:partial; 5/f30ed4f90cf8e7afdcb4f190339b0bed:partial; 22/a9fca93e44515de66d63662cfbeec98d:partial; 23/757745c610fa384dd6547023f3d3d9e2:partial |
| vietmed-1 | 25 | 2 | 0 | 13/42f9b05b7d8c1df53a326cc0923f813d:partial; 26/c67408e1d13e15e8a7ca95da15a80d9b:partial |
| vietmed-2 | 26 | 1 | 0 | 26/c67408e1d13e15e8a7ca95da15a80d9b:partial |
| vietmed-3 | 26 | 1 | 0 | 26/c67408e1d13e15e8a7ca95da15a80d9b:partial |
| vietmed-clear-1 | 4 | 0 | 0 | none |
| vietmed-noise-1 | 0 | 1 | 0 | 24/c0308753e444bd84a988eb04ef6012d7:partial |
| vimedcss-1 | 27 | 0 | 0 | none |
| vivos-1 | 25 | 2 | 0 | 2/71c31a2ef717d7ad599cf372283091e8:partial; 11/bef8e44423209db8a3d2d81b738140cf:partial |

## Per-bullet evidence

### librispeech-1

Bullet aggregate: 22 supported, 4 partial, 0 unsupported; certainty promotions 4; exact duplicates 0; source-overlap pairs 1.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| key_topics[0] | PARTIAL | YES | partial | no | no | c1a143de58f362dd570d203dd7adbb0e | c1a143de58f362dd570d203dd7adbb0e: MISTER QUILTER IS THE APOSTLE OF THE MIDDLE CLASSES AND WE ARE GLAD TO WELCOME HIS GOSPEL Note: The source says the speaker is glad to welcome the 'gospel'; 'praised' adds evaluative tone that the gold leaves satirical... |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | df5532e9050148ab2c3722971ad81114 | df5532e9050148ab2c3722971ad81114: NOR IS MISTER QUILTER'S MANNER LESS INTERESTING THAN HIS MATTER |
| key_topics[2] | SUPPORTED | no | sufficient | no | no | 7b1a256fe41aedba28a287c9d4d43272 | 7b1a256fe41aedba28a287c9d4d43272: HE TELLS US THAT AT THIS FESTIVE SEASON OF THE YEAR WITH CHRISTMAS AND ROAST BEEF LOOMING BEFORE US SIMILES DRAWN FROM EAT... |
| key_topics[3] | SUPPORTED | no | sufficient | no | no | 1a74365a942bb409125d39486af93d63 | 1a74365a942bb409125d39486af93d63: HE HAS GRAVE DOUBTS WHETHER SIR FREDERICK LEIGHTON'S WORK IS REALLY GREEK AFTER ALL AND CAN DISCOVER IN IT BUT LITTLE OF R... |
| key_topics[4] | SUPPORTED | no | sufficient | no | no | 37a86522e3ca280c373c8ee0361e8290 | 37a86522e3ca280c373c8ee0361e8290: LINNELL'S PICTURES ARE A SORT OF UP GUARDS AND AT EM PAINTINGS AND MASON'S EXQUISITE IDYLLS ARE AS NATIONAL AS A JINGO POE... |
| key_topics[5] | SUPPORTED | no | sufficient | no | no | 37a86522e3ca280c373c8ee0361e8290 | 37a86522e3ca280c373c8ee0361e8290: LINNELL'S PICTURES ARE A SORT OF UP GUARDS AND AT EM PAINTINGS AND MASON'S EXQUISITE IDYLLS ARE AS NATIONAL AS A JINGO POE... |
| key_topics[6] | PARTIAL | YES | partial | no | no | f30ed4f90cf8e7afdcb4f190339b0bed | f30ed4f90cf8e7afdcb4f190339b0bed: IT IS OBVIOUSLY UNNECESSARY FOR US TO POINT OUT HOW LUMINOUS THESE CRITICISMS ARE HOW DELICATE IN EXPRESSION Note: Luminous and delicate are source words; 'ironically' is not explicit and the gold leaves tone unresolved. |
| key_topics[7] | SUPPORTED | no | sufficient | no | no | c5b32dd679a3e4a51b0d971cd3f22d80 | c5b32dd679a3e4a51b0d971cd3f22d80: ON THE GENERAL PRINCIPLES OF ART MISTER QUILTER WRITES WITH EQUAL LUCIDITY |
| key_topics[8] | SUPPORTED | no | sufficient | no | no | 926dc428af02e36b0eb76b460b4e58f4 | 926dc428af02e36b0eb76b460b4e58f4: PAINTING HE TELLS US IS OF A DIFFERENT QUALITY TO MATHEMATICS AND FINISH IN ART IS ADDING MORE FACT |
| key_topics[9] | SUPPORTED | no | sufficient | no | no | 189f9b998d1118c0cb6867a49d6310cd | 189f9b998d1118c0cb6867a49d6310cd: AS FOR ETCHINGS THEY ARE OF TWO KINDS BRITISH AND FOREIGN |
| key_topics[10] | SUPPORTED | no | sufficient | no | no | 1c373df69d0ee3d2703d8599eac1c70b | 1c373df69d0ee3d2703d8599eac1c70b: HE LAMENTS MOST BITTERLY THE DIVORCE THAT HAS BEEN MADE BETWEEN DECORATIVE ART AND WHAT WE USUALLY CALL PICTURES MAKES THE... |
| key_topics[11] | SUPPORTED | no | sufficient | no | no | 146de65b4e33feb75200461c31cf1e02 | 146de65b4e33feb75200461c31cf1e02: NEAR THE FIRE AND THE ORNAMENTS FRED BROUGHT HOME FROM INDIA ON THE MANTEL BOARD |
| key_topics[12] | SUPPORTED | no | sufficient | no | no | d7c4a9cc622f93145bbe3ad2c87e5590 | d7c4a9cc622f93145bbe3ad2c87e5590: IN FACT HE IS QUITE SEVERE ON MISTER RUSKIN FOR NOT RECOGNISING THAT A PICTURE SHOULD DENOTE THE FRAILTY OF MAN AND REMARK... |
| key_topics[13] | SUPPORTED | no | sufficient | no | no | 027c76935d129746bbfddbf47117b4bd<br>40f06c858b1506309c57c7bd00a5fefb | 027c76935d129746bbfddbf47117b4bd: ONLY UNFORTUNATELY HIS OWN WORK NEVER DOES GET GOOD; 40f06c858b1506309c57c7bd00a5fefb: MISTER QUILTER HAS MISSED HIS CHANCE FOR HE HAS FAILED EVEN TO MAKE HIMSELF THE TUPPER OF PAINTING |
| key_topics[14] | SUPPORTED | no | sufficient | no | no | 74dc54b4ce05879227099a8360cd7483 | 74dc54b4ce05879227099a8360cd7483: BY HARRY QUILTER M A |
| key_topics[15] | SUPPORTED | no | sufficient | no | no | 4239b63002b9636fd6a41a9c43d2a910 | 4239b63002b9636fd6a41a9c43d2a910: BECAUSE YOU WERE SLEEPING INSTEAD OF CONQUERING THE LOVELY ROSE PRINCESS HAS BECOME A FIDDLE WITHOUT A BOW WHILE POOR SHAG... |
| key_topics[16] | SUPPORTED | no | sufficient | no | no | 991a3424602e72c0c4848e516321af23 | 991a3424602e72c0c4848e516321af23: HE HAS GONE AND GONE FOR GOOD ANSWERED POLYCHROME WHO HAD MANAGED TO SQUEEZE INTO THE ROOM BESIDE THE DRAGON AND HAD WITNE... |
| key_topics[17] | SUPPORTED | no | sufficient | no | no | e57d00eae1b6b9c132cc8c2a5c225cd1 | e57d00eae1b6b9c132cc8c2a5c225cd1: I HAVE REMAINED A PRISONER ONLY BECAUSE I WISHED TO BE ONE AND WITH THIS HE STEPPED FORWARD AND BURST THE STOUT CHAINS AS ... |
| key_topics[18] | SUPPORTED | no | sufficient | no | no | babe5ffd66afc0fb53216c81e86d8acd | babe5ffd66afc0fb53216c81e86d8acd: THE LITTLE GIRL HAD BEEN ASLEEP BUT SHE HEARD THE RAPS AND OPENED THE DOOR |
| key_topics[19] | SUPPORTED | no | sufficient | no | no | 95b9cf519605390e8acfb5eab4c9b355 | 95b9cf519605390e8acfb5eab4c9b355: THE KING HAS FLED IN DISGRACE AND YOUR FRIENDS ARE ASKING FOR YOU |
| key_topics[20] | SUPPORTED | no | sufficient | no | no | 12affa63374108fec19a1d8d37c333d1 | 12affa63374108fec19a1d8d37c333d1: I BEGGED RUGGEDO LONG AGO TO SEND HIM AWAY BUT HE WOULD NOT DO SO |
| key_topics[21] | SUPPORTED | no | sufficient | no | no | 5156d06a3db6c84d45a2997aeaf329fe | 5156d06a3db6c84d45a2997aeaf329fe: I ALSO OFFERED TO HELP YOUR BROTHER TO ESCAPE BUT HE WOULD NOT GO |
| key_topics[22] | PARTIAL | YES | partial | no | no | a9fca93e44515de66d63662cfbeec98d | a9fca93e44515de66d63662cfbeec98d: HE EATS AND SLEEPS VERY STEADILY REPLIED THE NEW KING Note: The cited segment says 'he' eats and sleeps; 'brother' requires an uncited antecedent/context. |
| key_topics[23] | PARTIAL | YES | partial | no | no | 757745c610fa384dd6547023f3d3d9e2<br>e5afda23b1c17fec802a243807624b28 | 757745c610fa384dd6547023f3d3d9e2: I HOPE HE DOESN'T WORK TOO HARD SAID SHAGGY; e5afda23b1c17fec802a243807624b28: HE DOESN'T WORK AT ALL Note: Both cited fragments support hope/no work, but neither names the person as the brother. |
| key_topics[24] | SUPPORTED | no | sufficient | no | no | fccf78947f24cf33239d921ff4539d92 | fccf78947f24cf33239d921ff4539d92: IN FACT THERE IS NOTHING HE CAN DO IN THESE DOMINIONS AS WELL AS OUR NOMES WHOSE NUMBERS ARE SO GREAT THAT IT WORRIES US T... |
| key_topics[25] | SUPPORTED | no | sufficient | no | no | 593e7d538141c188aa543b023d440fcd | 593e7d538141c188aa543b023d440fcd: NOT EXACTLY RETURNED KALIKO |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vietmed-1

Bullet aggregate: 10 supported, 1 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 4.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| overview[0] | SUPPORTED | no | sufficient | no | no | 6ab4e4b4d5f63c11cef670b80a0008e1<br>e6c768b08ed6366175dc06c14cc4a65b | 6ab4e4b4d5f63c11cef670b80a0008e1: cái khái niệm nha bây giờ rong kinh tức là cái ngày mà bình thường mình có kinh; e6c768b08ed6366175dc06c14cc4a65b: bây giờ mình sẽ đi vào phần phần nguyên nhân thì rong kinh và rong |
| key_topics[0] | SUPPORTED | no | sufficient | no | no | a91b18631845e2e2ce37765237244231<br>8939bedadd53f8c6216b974439cfeb74 | a91b18631845e2e2ce37765237244231: là tự từ ba ngày đến năm ngày một số trường hợp thì có thể là nhiều; 8939bedadd53f8c6216b974439cfeb74: lắm thì bảy ngày nhưng mà những cái ngày những ngày ngày từ ngày năm đến ngày bảy thì cái lượng máu mới ít hơn bây giờ ron... |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | 3c7319ae10f9b14415f1250028922691<br>a772f3b96ba0c4c7c601c809d526d1e7 | 3c7319ae10f9b14415f1250028922691: là cái cành chanh nó kéo dài trên bảy ngày; a772f3b96ba0c4c7c601c809d526d1e7: đúng ngày kinh của mình nhưng mà ngày kinh kéo dài trên bảy ngày gọi là rong kinh còn rong huyết tức là cái hiện tượng ra |
| key_topics[2] | SUPPORTED | no | sufficient | no | no | a772f3b96ba0c4c7c601c809d526d1e7<br>67ba1d1ed2309bf61fd2a0707fa5c3ff<br>51c7a4b34e67984705cbfeb2fc6d1016 | a772f3b96ba0c4c7c601c809d526d1e7: đúng ngày kinh của mình nhưng mà ngày kinh kéo dài trên bảy ngày gọi là rong kinh còn rong huyết tức là cái hiện tượng ra; 67ba1d1ed2309bf61fd2a0707fa5c3ff: máu âm đạo bất thường không có đúng ngày kinh nhưng mà kéo dài thì gọi là là; 51c7a4b34e67984705cbfeb2fc6d1016: rong huyết như vậy mình sẽ phân biệt với rong kinh và rong huyết |
| key_topics[3] | SUPPORTED | no | sufficient | no | no | e6c768b08ed6366175dc06c14cc4a65b<br>e72f24bb125fca8ca118b9e9bac0441d | e6c768b08ed6366175dc06c14cc4a65b: bây giờ mình sẽ đi vào phần phần nguyên nhân thì rong kinh và rong; e72f24bb125fca8ca118b9e9bac0441d: huyết thì nó số các nguyên nhân có thể là nguyên nhân nhằm cơ năng hoặc là nguyên nhân thực thể cơ năng tức là |
| key_topics[4] | SUPPORTED | no | sufficient | no | no | e72f24bb125fca8ca118b9e9bac0441d<br>3838666b1d730a8347a069da4355b185<br>f89d49bbc1ec6d583b3436ff1e9c7070<br>7eda6cb6fbfef6acfabd86d5d45025e2<br>57a88e0f24a272a1a1129590fdb37cd1 | e72f24bb125fca8ca118b9e9bac0441d: huyết thì nó số các nguyên nhân có thể là nguyên nhân nhằm cơ năng hoặc là nguyên nhân thực thể cơ năng tức là; 3838666b1d730a8347a069da4355b185: tử cung phần phụ là hoàn toàn bình thường không có bệnh lý gì cả nhưng mà chỉ do do rối loạn về chức năng của; f89d49bbc1ec6d583b3436ff1e9c7070: buồng trứng buồng trứng nó tiết ra những hoocmon estrogen và progesterone nó có những cái rối loạn thì ảnh hưởng biểu; 7eda6cb6fbfef6acfabd86d5d45025e2: hiện kinh nguyệt nó sẽ không vấn đề hoặc là nó không đều mà; 57a88e0f24a272a1a1129590fdb37cd1: không có kinh hoặc là là ra máu âm đạo kéo dài và một số nguyên nhân thực thể tức là có những bệnh lý thực thực thực |
| key_topics[5] | SUPPORTED | no | sufficient | no | no | 57a88e0f24a272a1a1129590fdb37cd1<br>42f9b05b7d8c1df53a326cc0923f813d | 57a88e0f24a272a1a1129590fdb37cd1: không có kinh hoặc là là ra máu âm đạo kéo dài và một số nguyên nhân thực thể tức là có những bệnh lý thực thực thực; 42f9b05b7d8c1df53a326cc0923f813d: tế ở tử cung hoặc âm đạo hoặc là cổ của tử cung thì tí nữa mình sẽ sang cái phần thăm khám |
| key_topics[6] | SUPPORTED | no | sufficient | no | no | 270f6c23f373b6816f61b1a46c996a93<br>1162e34933dba1a353e707f089fd49d6<br>1620c21d9f1663f11725477be1ec095a<br>075ccc7c07650594c6f6797dcef46784 | 270f6c23f373b6816f61b1a46c996a93: mình nói là về nguyên nhân thực thể thì mình hay gặp nhất đó là u xơ tử cung; 1162e34933dba1a353e707f089fd49d6: thì đừng là gây nên rong huyết còn nếu mà u xơ tử cung nó nằm ở những; 1620c21d9f1663f11725477be1ec095a: vị trí khác trong cơ tử cung hoặc dưới với thân mạc thì nó ít gây nên cho một; 075ccc7c07650594c6f6797dcef46784: biến chứng rong kinh rong huyết hơn |
| key_topics[7] | SUPPORTED | no | sufficient | no | no | b11cc4fe2cd8b9f2ac11854fd02e168f<br>1e7941e5f5ed7f4eec65eb83b817fa2c | b11cc4fe2cd8b9f2ac11854fd02e168f: những cái polyp nội mạc tử cung và những cái bệnh lý là tăng sản nội mạc; 1e7941e5f5ed7f4eec65eb83b817fa2c: tử cung là ung thư nội mạc tử cung nó thể một số những cái trường hợp như |
| key_topics[8] | PARTIAL | no | partial | no | no | 7e5f7a9066bcb8f40d91132ebf5e5b6d<br>8661ef789ba0add23df865ee5f42779b<br>82e1d4e9bc438cb28d9fc5ce2697194a<br>537b08949b2e7f1446f4955933d4d87c<br>c0308753e444bd84a988eb04ef6012d7 | 7e5f7a9066bcb8f40d91132ebf5e5b6d: là bị chấn thương hoặc là dị vật đường sinh dục một số em bé một số người mới ở; 8661ef789ba0add23df865ee5f42779b: vùng nông thôn; 82e1d4e9bc438cb28d9fc5ce2697194a: xuống mấy đồng ruộng thì có thể là có bị dị vật như là những con vật như con đỉa gì đó mà nó nó vào đường sinh dục; 537b08949b2e7f1446f4955933d4d87c: bạn nó sẽ nằm ở chỗ âm đạo và nó sẽ gây chảy máu mau kéo dài thường xuyên; c0308753e444bd84a988eb04ef6012d7: không biết là nguyên nhân gì sau nay mình đặt bỏ việc nạp và do những Note: The cited segments support trauma, leech, and prolonged bleeding; the clipped c030 segment does not independently support ... |
| key_topics[9] | SUPPORTED | no | sufficient | no | no | c2ff12a510c8518f136b7d6387fc1abc<br>c67408e1d13e15e8a7ca95da15a80d9b | c2ff12a510c8518f136b7d6387fc1abc: cái mặt lý để bị nội khoa bệnh về rối loạn trong chảy máu thì nó gây nên một cái tình trạng là; c67408e1d13e15e8a7ca95da15a80d9b: rong kinh rong huyết kéo dài do không không cầm máu được ngoài ra thì do những |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vietmed-2

Bullet aggregate: 12 supported, 1 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 7.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| overview[0] | SUPPORTED | no | sufficient | no | no | 6ab4e4b4d5f63c11cef670b80a0008e1<br>51c7a4b34e67984705cbfeb2fc6d1016<br>e6c768b08ed6366175dc06c14cc4a65b | 6ab4e4b4d5f63c11cef670b80a0008e1: cái khái niệm nha bây giờ rong kinh tức là cái ngày mà bình thường mình có kinh; 51c7a4b34e67984705cbfeb2fc6d1016: rong huyết như vậy mình sẽ phân biệt với rong kinh và rong huyết; e6c768b08ed6366175dc06c14cc4a65b: bây giờ mình sẽ đi vào phần phần nguyên nhân thì rong kinh và rong |
| key_topics[0] | SUPPORTED | no | sufficient | no | no | 6ab4e4b4d5f63c11cef670b80a0008e1<br>a91b18631845e2e2ce37765237244231<br>8939bedadd53f8c6216b974439cfeb74 | 6ab4e4b4d5f63c11cef670b80a0008e1: cái khái niệm nha bây giờ rong kinh tức là cái ngày mà bình thường mình có kinh; a91b18631845e2e2ce37765237244231: là tự từ ba ngày đến năm ngày một số trường hợp thì có thể là nhiều; 8939bedadd53f8c6216b974439cfeb74: lắm thì bảy ngày nhưng mà những cái ngày những ngày ngày từ ngày năm đến ngày bảy thì cái lượng máu mới ít hơn bây giờ ron... |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | 3c7319ae10f9b14415f1250028922691<br>a772f3b96ba0c4c7c601c809d526d1e7 | 3c7319ae10f9b14415f1250028922691: là cái cành chanh nó kéo dài trên bảy ngày; a772f3b96ba0c4c7c601c809d526d1e7: đúng ngày kinh của mình nhưng mà ngày kinh kéo dài trên bảy ngày gọi là rong kinh còn rong huyết tức là cái hiện tượng ra |
| key_topics[2] | SUPPORTED | no | sufficient | no | no | a772f3b96ba0c4c7c601c809d526d1e7<br>67ba1d1ed2309bf61fd2a0707fa5c3ff<br>51c7a4b34e67984705cbfeb2fc6d1016 | a772f3b96ba0c4c7c601c809d526d1e7: đúng ngày kinh của mình nhưng mà ngày kinh kéo dài trên bảy ngày gọi là rong kinh còn rong huyết tức là cái hiện tượng ra; 67ba1d1ed2309bf61fd2a0707fa5c3ff: máu âm đạo bất thường không có đúng ngày kinh nhưng mà kéo dài thì gọi là là; 51c7a4b34e67984705cbfeb2fc6d1016: rong huyết như vậy mình sẽ phân biệt với rong kinh và rong huyết |
| key_topics[3] | SUPPORTED | no | sufficient | no | no | e6c768b08ed6366175dc06c14cc4a65b<br>e72f24bb125fca8ca118b9e9bac0441d | e6c768b08ed6366175dc06c14cc4a65b: bây giờ mình sẽ đi vào phần phần nguyên nhân thì rong kinh và rong; e72f24bb125fca8ca118b9e9bac0441d: huyết thì nó số các nguyên nhân có thể là nguyên nhân nhằm cơ năng hoặc là nguyên nhân thực thể cơ năng tức là |
| key_topics[4] | SUPPORTED | no | sufficient | no | no | e72f24bb125fca8ca118b9e9bac0441d<br>3838666b1d730a8347a069da4355b185<br>f89d49bbc1ec6d583b3436ff1e9c7070<br>7eda6cb6fbfef6acfabd86d5d45025e2<br>57a88e0f24a272a1a1129590fdb37cd1 | e72f24bb125fca8ca118b9e9bac0441d: huyết thì nó số các nguyên nhân có thể là nguyên nhân nhằm cơ năng hoặc là nguyên nhân thực thể cơ năng tức là; 3838666b1d730a8347a069da4355b185: tử cung phần phụ là hoàn toàn bình thường không có bệnh lý gì cả nhưng mà chỉ do do rối loạn về chức năng của; f89d49bbc1ec6d583b3436ff1e9c7070: buồng trứng buồng trứng nó tiết ra những hoocmon estrogen và progesterone nó có những cái rối loạn thì ảnh hưởng biểu; 7eda6cb6fbfef6acfabd86d5d45025e2: hiện kinh nguyệt nó sẽ không vấn đề hoặc là nó không đều mà; 57a88e0f24a272a1a1129590fdb37cd1: không có kinh hoặc là là ra máu âm đạo kéo dài và một số nguyên nhân thực thể tức là có những bệnh lý thực thực thực |
| key_topics[5] | SUPPORTED | no | sufficient | no | no | 57a88e0f24a272a1a1129590fdb37cd1<br>42f9b05b7d8c1df53a326cc0923f813d | 57a88e0f24a272a1a1129590fdb37cd1: không có kinh hoặc là là ra máu âm đạo kéo dài và một số nguyên nhân thực thể tức là có những bệnh lý thực thực thực; 42f9b05b7d8c1df53a326cc0923f813d: tế ở tử cung hoặc âm đạo hoặc là cổ của tử cung thì tí nữa mình sẽ sang cái phần thăm khám |
| key_topics[6] | SUPPORTED | no | sufficient | no | no | 270f6c23f373b6816f61b1a46c996a93<br>1162e34933dba1a353e707f089fd49d6<br>1620c21d9f1663f11725477be1ec095a<br>075ccc7c07650594c6f6797dcef46784 | 270f6c23f373b6816f61b1a46c996a93: mình nói là về nguyên nhân thực thể thì mình hay gặp nhất đó là u xơ tử cung; 1162e34933dba1a353e707f089fd49d6: thì đừng là gây nên rong huyết còn nếu mà u xơ tử cung nó nằm ở những; 1620c21d9f1663f11725477be1ec095a: vị trí khác trong cơ tử cung hoặc dưới với thân mạc thì nó ít gây nên cho một; 075ccc7c07650594c6f6797dcef46784: biến chứng rong kinh rong huyết hơn |
| key_topics[7] | SUPPORTED | no | sufficient | no | no | b11cc4fe2cd8b9f2ac11854fd02e168f<br>1e7941e5f5ed7f4eec65eb83b817fa2c | b11cc4fe2cd8b9f2ac11854fd02e168f: những cái polyp nội mạc tử cung và những cái bệnh lý là tăng sản nội mạc; 1e7941e5f5ed7f4eec65eb83b817fa2c: tử cung là ung thư nội mạc tử cung nó thể một số những cái trường hợp như |
| key_topics[8] | SUPPORTED | no | sufficient | no | no | 7e5f7a9066bcb8f40d91132ebf5e5b6d | 7e5f7a9066bcb8f40d91132ebf5e5b6d: là bị chấn thương hoặc là dị vật đường sinh dục một số em bé một số người mới ở |
| key_topics[9] | PARTIAL | no | partial | no | no | 8661ef789ba0add23df865ee5f42779b<br>82e1d4e9bc438cb28d9fc5ce2697194a<br>537b08949b2e7f1446f4955933d4d87c<br>c0308753e444bd84a988eb04ef6012d7 | 8661ef789ba0add23df865ee5f42779b: vùng nông thôn; 82e1d4e9bc438cb28d9fc5ce2697194a: xuống mấy đồng ruộng thì có thể là có bị dị vật như là những con vật như con đỉa gì đó mà nó nó vào đường sinh dục; 537b08949b2e7f1446f4955933d4d87c: bạn nó sẽ nằm ở chỗ âm đạo và nó sẽ gây chảy máu mau kéo dài thường xuyên; c0308753e444bd84a988eb04ef6012d7: không biết là nguyên nhân gì sau nay mình đặt bỏ việc nạp và do những Note: The clean source IDs support the leech/bleeding claim; c030 is explicitly unintelligible, so the grouped statement is only... |
| key_topics[10] | SUPPORTED | no | sufficient | no | no | c2ff12a510c8518f136b7d6387fc1abc<br>c67408e1d13e15e8a7ca95da15a80d9b | c2ff12a510c8518f136b7d6387fc1abc: cái mặt lý để bị nội khoa bệnh về rối loạn trong chảy máu thì nó gây nên một cái tình trạng là; c67408e1d13e15e8a7ca95da15a80d9b: rong kinh rong huyết kéo dài do không không cầm máu được ngoài ra thì do những |
| confirmation_items[0] | SUPPORTED | no | sufficient | no | no | 42f9b05b7d8c1df53a326cc0923f813d | 42f9b05b7d8c1df53a326cc0923f813d: tế ở tử cung hoặc âm đạo hoặc là cổ của tử cung thì tí nữa mình sẽ sang cái phần thăm khám |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vietmed-3

Bullet aggregate: 11 supported, 1 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 7.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| overview[0] | SUPPORTED | no | sufficient | no | no | 6ab4e4b4d5f63c11cef670b80a0008e1<br>51c7a4b34e67984705cbfeb2fc6d1016<br>e6c768b08ed6366175dc06c14cc4a65b | 6ab4e4b4d5f63c11cef670b80a0008e1: cái khái niệm nha bây giờ rong kinh tức là cái ngày mà bình thường mình có kinh; 51c7a4b34e67984705cbfeb2fc6d1016: rong huyết như vậy mình sẽ phân biệt với rong kinh và rong huyết; e6c768b08ed6366175dc06c14cc4a65b: bây giờ mình sẽ đi vào phần phần nguyên nhân thì rong kinh và rong |
| key_topics[0] | SUPPORTED | no | sufficient | no | no | 6ab4e4b4d5f63c11cef670b80a0008e1<br>a91b18631845e2e2ce37765237244231<br>8939bedadd53f8c6216b974439cfeb74 | 6ab4e4b4d5f63c11cef670b80a0008e1: cái khái niệm nha bây giờ rong kinh tức là cái ngày mà bình thường mình có kinh; a91b18631845e2e2ce37765237244231: là tự từ ba ngày đến năm ngày một số trường hợp thì có thể là nhiều; 8939bedadd53f8c6216b974439cfeb74: lắm thì bảy ngày nhưng mà những cái ngày những ngày ngày từ ngày năm đến ngày bảy thì cái lượng máu mới ít hơn bây giờ ron... |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | 3c7319ae10f9b14415f1250028922691<br>a772f3b96ba0c4c7c601c809d526d1e7 | 3c7319ae10f9b14415f1250028922691: là cái cành chanh nó kéo dài trên bảy ngày; a772f3b96ba0c4c7c601c809d526d1e7: đúng ngày kinh của mình nhưng mà ngày kinh kéo dài trên bảy ngày gọi là rong kinh còn rong huyết tức là cái hiện tượng ra |
| key_topics[2] | SUPPORTED | no | sufficient | no | no | a772f3b96ba0c4c7c601c809d526d1e7<br>67ba1d1ed2309bf61fd2a0707fa5c3ff<br>51c7a4b34e67984705cbfeb2fc6d1016 | a772f3b96ba0c4c7c601c809d526d1e7: đúng ngày kinh của mình nhưng mà ngày kinh kéo dài trên bảy ngày gọi là rong kinh còn rong huyết tức là cái hiện tượng ra; 67ba1d1ed2309bf61fd2a0707fa5c3ff: máu âm đạo bất thường không có đúng ngày kinh nhưng mà kéo dài thì gọi là là; 51c7a4b34e67984705cbfeb2fc6d1016: rong huyết như vậy mình sẽ phân biệt với rong kinh và rong huyết |
| key_topics[3] | SUPPORTED | no | sufficient | no | no | e6c768b08ed6366175dc06c14cc4a65b<br>e72f24bb125fca8ca118b9e9bac0441d | e6c768b08ed6366175dc06c14cc4a65b: bây giờ mình sẽ đi vào phần phần nguyên nhân thì rong kinh và rong; e72f24bb125fca8ca118b9e9bac0441d: huyết thì nó số các nguyên nhân có thể là nguyên nhân nhằm cơ năng hoặc là nguyên nhân thực thể cơ năng tức là |
| key_topics[4] | SUPPORTED | no | sufficient | no | no | e72f24bb125fca8ca118b9e9bac0441d<br>3838666b1d730a8347a069da4355b185<br>f89d49bbc1ec6d583b3436ff1e9c7070<br>7eda6cb6fbfef6acfabd86d5d45025e2<br>57a88e0f24a272a1a1129590fdb37cd1 | e72f24bb125fca8ca118b9e9bac0441d: huyết thì nó số các nguyên nhân có thể là nguyên nhân nhằm cơ năng hoặc là nguyên nhân thực thể cơ năng tức là; 3838666b1d730a8347a069da4355b185: tử cung phần phụ là hoàn toàn bình thường không có bệnh lý gì cả nhưng mà chỉ do do rối loạn về chức năng của; f89d49bbc1ec6d583b3436ff1e9c7070: buồng trứng buồng trứng nó tiết ra những hoocmon estrogen và progesterone nó có những cái rối loạn thì ảnh hưởng biểu; 7eda6cb6fbfef6acfabd86d5d45025e2: hiện kinh nguyệt nó sẽ không vấn đề hoặc là nó không đều mà; 57a88e0f24a272a1a1129590fdb37cd1: không có kinh hoặc là là ra máu âm đạo kéo dài và một số nguyên nhân thực thể tức là có những bệnh lý thực thực thực |
| key_topics[5] | SUPPORTED | no | sufficient | no | no | 57a88e0f24a272a1a1129590fdb37cd1<br>42f9b05b7d8c1df53a326cc0923f813d | 57a88e0f24a272a1a1129590fdb37cd1: không có kinh hoặc là là ra máu âm đạo kéo dài và một số nguyên nhân thực thể tức là có những bệnh lý thực thực thực; 42f9b05b7d8c1df53a326cc0923f813d: tế ở tử cung hoặc âm đạo hoặc là cổ của tử cung thì tí nữa mình sẽ sang cái phần thăm khám |
| key_topics[6] | SUPPORTED | no | sufficient | no | no | 270f6c23f373b6816f61b1a46c996a93<br>1162e34933dba1a353e707f089fd49d6<br>1620c21d9f1663f11725477be1ec095a<br>075ccc7c07650594c6f6797dcef46784 | 270f6c23f373b6816f61b1a46c996a93: mình nói là về nguyên nhân thực thể thì mình hay gặp nhất đó là u xơ tử cung; 1162e34933dba1a353e707f089fd49d6: thì đừng là gây nên rong huyết còn nếu mà u xơ tử cung nó nằm ở những; 1620c21d9f1663f11725477be1ec095a: vị trí khác trong cơ tử cung hoặc dưới với thân mạc thì nó ít gây nên cho một; 075ccc7c07650594c6f6797dcef46784: biến chứng rong kinh rong huyết hơn |
| key_topics[7] | SUPPORTED | no | sufficient | no | no | b11cc4fe2cd8b9f2ac11854fd02e168f<br>1e7941e5f5ed7f4eec65eb83b817fa2c | b11cc4fe2cd8b9f2ac11854fd02e168f: những cái polyp nội mạc tử cung và những cái bệnh lý là tăng sản nội mạc; 1e7941e5f5ed7f4eec65eb83b817fa2c: tử cung là ung thư nội mạc tử cung nó thể một số những cái trường hợp như |
| key_topics[8] | PARTIAL | no | partial | no | no | 7e5f7a9066bcb8f40d91132ebf5e5b6d<br>8661ef789ba0add23df865ee5f42779b<br>82e1d4e9bc438cb28d9fc5ce2697194a<br>537b08949b2e7f1446f4955933d4d87c<br>c0308753e444bd84a988eb04ef6012d7 | 7e5f7a9066bcb8f40d91132ebf5e5b6d: là bị chấn thương hoặc là dị vật đường sinh dục một số em bé một số người mới ở; 8661ef789ba0add23df865ee5f42779b: vùng nông thôn; 82e1d4e9bc438cb28d9fc5ce2697194a: xuống mấy đồng ruộng thì có thể là có bị dị vật như là những con vật như con đỉa gì đó mà nó nó vào đường sinh dục; 537b08949b2e7f1446f4955933d4d87c: bạn nó sẽ nằm ở chỗ âm đạo và nó sẽ gây chảy máu mau kéo dài thường xuyên; c0308753e444bd84a988eb04ef6012d7: không biết là nguyên nhân gì sau nay mình đặt bỏ việc nạp và do những Note: The cited segments support trauma, leech, and prolonged bleeding; the clipped c030 segment does not independently support ... |
| key_topics[9] | SUPPORTED | no | sufficient | no | no | c2ff12a510c8518f136b7d6387fc1abc<br>c67408e1d13e15e8a7ca95da15a80d9b | c2ff12a510c8518f136b7d6387fc1abc: cái mặt lý để bị nội khoa bệnh về rối loạn trong chảy máu thì nó gây nên một cái tình trạng là; c67408e1d13e15e8a7ca95da15a80d9b: rong kinh rong huyết kéo dài do không không cầm máu được ngoài ra thì do những |
| confirmation_items[0] | SUPPORTED | no | sufficient | no | no | 42f9b05b7d8c1df53a326cc0923f813d | 42f9b05b7d8c1df53a326cc0923f813d: tế ở tử cung hoặc âm đạo hoặc là cổ của tử cung thì tí nữa mình sẽ sang cái phần thăm khám |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vietmed-clear-1

Bullet aggregate: 4 supported, 0 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 2.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| overview[0] | SUPPORTED | no | sufficient | no | no | 7e5f7a9066bcb8f40d91132ebf5e5b6d | 7e5f7a9066bcb8f40d91132ebf5e5b6d: là bị chấn thương hoặc là dị vật đường sinh dục một số em bé một số người mới ở |
| key_topics[0] | SUPPORTED | no | sufficient | no | no | 7e5f7a9066bcb8f40d91132ebf5e5b6d<br>8661ef789ba0add23df865ee5f42779b<br>82e1d4e9bc438cb28d9fc5ce2697194a | 7e5f7a9066bcb8f40d91132ebf5e5b6d: là bị chấn thương hoặc là dị vật đường sinh dục một số em bé một số người mới ở; 8661ef789ba0add23df865ee5f42779b: vùng nông thôn; 82e1d4e9bc438cb28d9fc5ce2697194a: xuống mấy đồng ruộng thì có thể là có bị dị vật như là những con vật như con đỉa gì đó mà nó nó vào đường sinh dục |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | 82e1d4e9bc438cb28d9fc5ce2697194a | 82e1d4e9bc438cb28d9fc5ce2697194a: xuống mấy đồng ruộng thì có thể là có bị dị vật như là những con vật như con đỉa gì đó mà nó nó vào đường sinh dục |
| key_topics[2] | SUPPORTED | no | sufficient | no | no | 537b08949b2e7f1446f4955933d4d87c | 537b08949b2e7f1446f4955933d4d87c: bạn nó sẽ nằm ở chỗ âm đạo và nó sẽ gây chảy máu mau kéo dài thường xuyên |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vietmed-noise-1

Bullet aggregate: 0 supported, 1 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 0.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| open_questions[0] | PARTIAL | no | partial | no | no | c0308753e444bd84a988eb04ef6012d7 | c0308753e444bd84a988eb04ef6012d7: không biết là nguyên nhân gì sau nay mình đặt bỏ việc nạp và do những Note: The unknown-cause/fragment framing is supported; glossing 'nạp' as loading/top-up is not supported by the clipped source. |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vimedcss-1

Bullet aggregate: 26 supported, 1 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 2.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| key_topics[0] | SUPPORTED | no | sufficient | no | no | 0101246d57235d6f5d1405f82e7076ed | 0101246d57235d6f5d1405f82e7076ed: Điều này có nghĩa là để xuất hiện bệnh, cả hai bản sao của gen phải bị đột biến và bệnh nhân phải là nam giới về mặt di tr... |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | 31f1743e95bf9d70b1e952426281262b | 31f1743e95bf9d70b1e952426281262b: Rồi tiếp theo, về lợi khuẩn thì sản phẩm Alpha lipid trước đây đã được trang bị rất tốt với cái hàm lượng lợi khuẩn là 1 t... |
| key_topics[2] | SUPPORTED | no | sufficient | no | no | 0d53b8b0b8459a84a1548b6cdb98b054 | 0d53b8b0b8459a84a1548b6cdb98b054: Rồi về chất khoáng ấy thì Alpha lipid thì có ba loại khoáng chất chính, đó là sắt, đó là magie và đó là canxi. |
| key_topics[3] | SUPPORTED | no | sufficient | no | no | 480547a5e412d867fcffd3f5879e882b<br>90206af5c3dc045c0b4ffcae0139fb2e | 480547a5e412d867fcffd3f5879e882b: Và chúng ta thấy là đối với người bình thường thì methionin lại rất là tốt.; 90206af5c3dc045c0b4ffcae0139fb2e: Đây là những cái nghiên cứu, các nghiên cứu trên thế giới cập nhật gần đây đã cho thấy là chức năng của methionin giúp cho... |
| key_topics[4] | SUPPORTED | no | sufficient | no | no | 618b67d278b4a029d35279dc0af74b13 | 618b67d278b4a029d35279dc0af74b13: Không được dùng methionin để điều trị ngộ độc paracetamol nếu đã quá 12 giờ tính từ lúc uống thuốc quá liều. |
| key_topics[5] | SUPPORTED | no | sufficient | no | no | 2c5c911ade433779c1dfc7c6cc09bd37 | 2c5c911ade433779c1dfc7c6cc09bd37: methionin có thể làm giảm tác dụng của Levodopa. |
| key_topics[6] | SUPPORTED | no | sufficient | no | no | 061f631f81eb192a7091cc6037e389ff | 061f631f81eb192a7091cc6037e389ff: Do đôi lúc gây tăng hoặc giảm hoạt động của estrogen, tác hại của tình trạng mất cân bằng nội tiết tố estrogen ở phụ nữ. |
| key_topics[7] | SUPPORTED | no | sufficient | no | no | 00db21c3cd76f3fad92a7e98a3a0e518 | 00db21c3cd76f3fad92a7e98a3a0e518: Sau đó, hàm lượng hormone sinh dục nữ này sẽ từ từ hạ dần về mức bình thường, các loại hormone estrogen, thực tế. |
| key_topics[8] | SUPPORTED | no | sufficient | no | no | dbfd261ec946fa925458e92335b4bfbd | dbfd261ec946fa925458e92335b4bfbd: Tại viện Wistar năm 1958, Hayflick bắt đầu nghiên cứu xem liệu virus có thể gây ung thư ở người hay không. |
| key_topics[9] | SUPPORTED | no | sufficient | no | no | 07190b92c77b968008da57e30b84ef58 | 07190b92c77b968008da57e30b84ef58: Tuy nhiên đối với hai cái thể bệnh đó đối với trường hợp bệnh Basedow thì cái lứa tuổi thường gặp là từ 20 đến 50 tuổi. Và... |
| key_topics[10] | SUPPORTED | no | sufficient | no | no | 577f0427490aa46e40bdfb1472159f9a | 577f0427490aa46e40bdfb1472159f9a: Một số bệnh nhân mà cường giáp do bệnh nhân Basedow đó thì do xuất hiện những cái tự kháng thể này có thể tác động lên cả ... |
| key_topics[11] | SUPPORTED | no | sufficient | no | no | d3bff99bddd4b07cfed11f8b5f7d3403 | d3bff99bddd4b07cfed11f8b5f7d3403: Đối với phụ nữ mang thai, beta-glucan giúp tăng cường hệ thống miễn dịch, phòng chống nhiễm trùng vi khuẩn, virus, bảo vệ ... |
| key_topics[12] | SUPPORTED | no | sufficient | no | no | 01568f9e591934d30d049be5a7c072ce | 01568f9e591934d30d049be5a7c072ce: Tác dụng thứ hai nữa đó là tác dụng hạ lipid máu, giảm mỡ và cholesterol trong máu. |
| key_topics[13] | SUPPORTED | no | sufficient | no | no | 8eb34df7f81da2b88770080488827187 | 8eb34df7f81da2b88770080488827187: Bạn có thể biết đến nó là thành phần chính trong nước tẩy sơn móng tay. Vậy chúng ta tiếp xúc với aceton ở đâu? |
| key_topics[14] | SUPPORTED | no | sufficient | no | no | 7f6eb3dcb68afbcff9e51d58067213f2 | 7f6eb3dcb68afbcff9e51d58067213f2: Đó là thời gian tối thiểu để tạo ra hiệu ứng đóng băng tuổi. Cuộc cách mạng bổ sung colagen và NMN của đất nước mặt trời m... |
| key_topics[15] | SUPPORTED | no | sufficient | no | no | 7f6eb3dcb68afbcff9e51d58067213f2 | 7f6eb3dcb68afbcff9e51d58067213f2: Đó là thời gian tối thiểu để tạo ra hiệu ứng đóng băng tuổi. Cuộc cách mạng bổ sung colagen và NMN của đất nước mặt trời m... |
| key_topics[16] | SUPPORTED | no | sufficient | no | no | 996362c1bb4ee4461ca2b4e1fc455c53 | 996362c1bb4ee4461ca2b4e1fc455c53: Axit glycyrrhizic từ rễ cam thảo, glycin và DL-methionin. |
| key_topics[17] | SUPPORTED | no | sufficient | no | no | 8131b8df2dc60cebb79f0e6ca7d949ff | 8131b8df2dc60cebb79f0e6ca7d949ff: Sử dụng có trách nhiệm và xử lý đúng cách là chìa khóa để tận hưởng những lợi ích của aceton, đồng thời giảm thiểu rủi ro. |
| key_topics[18] | SUPPORTED | no | sufficient | no | no | 1c975c829b37be48aaf8404d1844b05e<br>89fb8c814b4ae2b4501cfb94416de26f | 1c975c829b37be48aaf8404d1844b05e: Sao nó lại là một chỉ số quan trọng? Tại vì nó phản ánh được cái nồng độ glucose máu ở trong trung bình ở trong 3 tháng đó.; 89fb8c814b4ae2b4501cfb94416de26f: Cứ như thế thì người ta sẽ tra từ bên trái qua bên phải, khi mà có chỉ số HbA1c thì chúng ta sẽ biết được cái nồng độ gluc... |
| key_topics[19] | PARTIAL | no | partial | no | no | 5159157f053ae413f10adab3e4439778 | 5159157f053ae413f10adab3e4439778: và đặc biệt rất là tuyệt vời với tất cả các chị em phụ nữ mà đang bị về những cái căn bệnh như là nấm Candida. Note: The source says an unspecified thing is wonderful for women with Candida; 'product or intervention' supplies an ungrounded... |
| key_topics[20] | SUPPORTED | no | sufficient | no | no | 61194bcbe58060b7a94b2967e04177bf | 61194bcbe58060b7a94b2967e04177bf: 550 là acid uric cao nhưng mà chưa gọi là gout. |
| key_topics[21] | SUPPORTED | no | sufficient | no | no | 030adae46303a18ca9b84d2495d024bd | 030adae46303a18ca9b84d2495d024bd: Tô phi là các cục u cứng nổi dưới da, khi này quá trình tích tụ của axit uric nó tăng cao, hình thành các tinh thể urat nổ... |
| key_topics[22] | SUPPORTED | no | sufficient | no | no | a9a311d9b55550a38d4c6392e981c525 | a9a311d9b55550a38d4c6392e981c525: Nó cũng chứa thành phần đạm cao, có chứa nhân purin cao, nó cũng không tốt cho những người bị bệnh gout. |
| key_topics[23] | SUPPORTED | no | sufficient | no | no | fa1b115e920d83e698899cd96e40049b | fa1b115e920d83e698899cd96e40049b: lipid, các lipid đơn thuần không có tính kháng nguyên. |
| key_topics[24] | SUPPORTED | no | sufficient | no | no | 529039f2a0b618045ed4f8e1eebf2d03 | 529039f2a0b618045ed4f8e1eebf2d03: Dòng hem 7600T. |
| open_questions[0] | SUPPORTED | no | sufficient | no | no | 779265ea115bcb29de2732999e4ee7e6 | 779265ea115bcb29de2732999e4ee7e6: Một số loại thuốc như steroid, ampicillin, thuốc có chứa estrogen, phenothiazine và tetracycline, chế độ ăn uống ảnh hưởng... |
| open_questions[1] | SUPPORTED | no | sufficient | no | no | 8eb34df7f81da2b88770080488827187 | 8eb34df7f81da2b88770080488827187: Bạn có thể biết đến nó là thành phần chính trong nước tẩy sơn móng tay. Vậy chúng ta tiếp xúc với aceton ở đâu? |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

### vivos-1

Bullet aggregate: 25 supported, 2 partial, 0 unsupported; certainty promotions 0; exact duplicates 0; source-overlap pairs 0.

| section/index | support | certainty promotion | citation sufficiency | owner/deadline invented | exact duplicate | cited source IDs | short source evidence |
|---|---|---|---|---|---|---|---|
| key_topics[0] | SUPPORTED | no | sufficient | no | no | 43df003517ab06d4246e16b99837104b | 43df003517ab06d4246e16b99837104b: Tuy nhiên, ca ghép đã hoàn tất như dự kiến, ông Liêm nói. |
| key_topics[1] | SUPPORTED | no | sufficient | no | no | 921d72e48cfcdc8d77125af9b0a02a5b | 921d72e48cfcdc8d77125af9b0a02a5b: Có nhiều người tôi còn nhớ được cả tên |
| key_topics[2] | PARTIAL | no | partial | no | no | 71c31a2ef717d7ad599cf372283091e8 | 71c31a2ef717d7ad599cf372283091e8: Ăn ở có nhân có nghĩa. Note: The idiom supports humane/moral kindness; 'loyalty' is not explicit in the cited segment. |
| key_topics[3] | SUPPORTED | no | sufficient | no | no | 1da6a216e8c6423c347025352de15bd3 | 1da6a216e8c6423c347025352de15bd3: Những loại thực phẩm này |
| key_topics[4] | SUPPORTED | no | sufficient | no | no | 50035c923509fe7fbf71e983234a21e4 | 50035c923509fe7fbf71e983234a21e4: Hai mươi sáu, hai mươi bảy. |
| key_topics[5] | SUPPORTED | no | sufficient | no | no | f332b3337e333ba02a3df6dd15e8fa8b | f332b3337e333ba02a3df6dd15e8fa8b: Sự buồn tẻ trong công việc có thể trở thành mảnh đất màu mỡ cho hoạt động sáng tạo. |
| key_topics[6] | SUPPORTED | no | sufficient | no | no | 48c8a39380a3fdd09752e81edb893044 | 48c8a39380a3fdd09752e81edb893044: Nhiều điểm nóng đã hạ nhiệt. |
| key_topics[7] | SUPPORTED | no | sufficient | no | no | 3521cb0fabdfdb5b4b8ef7a940af278d | 3521cb0fabdfdb5b4b8ef7a940af278d: Học bổng nghiên cứu sinh sau tiến sĩ tại Hàn Quốc. |
| key_topics[8] | SUPPORTED | no | sufficient | no | no | 0dc76ba4c134aeca06dafa3afad78357 | 0dc76ba4c134aeca06dafa3afad78357: Trường Đại học Khoa học Tự nhiên Thành phố Hồ Chí Minh. |
| key_topics[9] | SUPPORTED | no | sufficient | no | no | e264b2f31bd52c20516a2a5a915d3012 | e264b2f31bd52c20516a2a5a915d3012: Cảnh sát không thông báo về những gì chứa trong hai gói này. |
| key_topics[10] | SUPPORTED | no | sufficient | no | no | 46cd478bfa545a5534f11369eae4372a | 46cd478bfa545a5534f11369eae4372a: Mà khi chúng ta nắm giữ được |
| key_topics[11] | PARTIAL | no | partial | no | no | bef8e44423209db8a3d2d81b738140cf | bef8e44423209db8a3d2d81b738140cf: hay thâm tím, chậm lành. Note: The source is a fragment naming bruising/slow healing without a subject; labeling it an unspecified condition adds a relat... |
| key_topics[12] | SUPPORTED | no | sufficient | no | no | aa2c08ad2164bd155ead778fd02ebf52 | aa2c08ad2164bd155ead778fd02ebf52: Mỗi mùa tuyển sinh cao đẳng, đại học là một mùa hội của làng Rồng. |
| key_topics[13] | SUPPORTED | no | sufficient | no | no | 73e4a239006dcacec4f2c3a1368e4df8 | 73e4a239006dcacec4f2c3a1368e4df8: Người người lại ra đồng, nhà nhà lại lo cấy hái. |
| key_topics[14] | SUPPORTED | no | sufficient | no | no | 31a6f364e1b3614aa985c980e3a0117b | 31a6f364e1b3614aa985c980e3a0117b: mang tính cơ hội trong thị hiếu pháp |
| key_topics[15] | SUPPORTED | no | sufficient | no | no | 6da3f19f91095786866f02d2e03df975 | 6da3f19f91095786866f02d2e03df975: Bốn mươi bốn, bốn mươi lăm. |
| key_topics[16] | SUPPORTED | no | sufficient | no | no | 5b60f91b2b922de5511b48de39b9b676 | 5b60f91b2b922de5511b48de39b9b676: Tôi muốn thời gian đó quan sát và tư duy, nhờ đó mà có ý tưởng điều chỉnh nhịp đèn, anh Tuấn chia sẻ. |
| key_topics[17] | SUPPORTED | no | sufficient | no | no | 810163a8f40f8e8e96108343bf747b16 | 810163a8f40f8e8e96108343bf747b16: nhờ những nhà hùng biện dí dóm, hài hước. |
| key_topics[18] | SUPPORTED | no | sufficient | no | no | 5151ac3b117b82d4944ccd99372db3b9 | 5151ac3b117b82d4944ccd99372db3b9: Con biết nịnh bố mỗi khi thích món đồ chơi nào đó. |
| key_topics[19] | SUPPORTED | no | sufficient | no | no | 5f662d73ba14a3ec88d166b6d26b9499 | 5f662d73ba14a3ec88d166b6d26b9499: Sáu mươi, sáu mươi mốt. |
| key_topics[20] | SUPPORTED | no | sufficient | no | no | 322820036179cb93257cc7271dafd03f | 322820036179cb93257cc7271dafd03f: Vừa qua cầu treo vừa câu cá (ảnh) Bốn chân di chuyển trên ba chiếc ghế nhựa bịt mắt qua cầu bập bênh đập niêu ném còn |
| key_topics[21] | SUPPORTED | no | sufficient | no | no | 693849e2f264f9e041b273d896f0930c | 693849e2f264f9e041b273d896f0930c: mà lại đứng bên ngoài cộng đồng. |
| key_topics[22] | SUPPORTED | no | sufficient | no | no | 9022edcecd00cb06f6997940677c5604 | 9022edcecd00cb06f6997940677c5604: Dạo đó, khi nghe có người chọc lá rụng về cội, ông Hai Chi rồi cũng chỉ một mình Chín Hoà tức lắm vì cậu rất thương ba nuôi. |
| key_topics[23] | SUPPORTED | no | sufficient | no | no | b61886b794c6ea6a583162498060f017 | b61886b794c6ea6a583162498060f017: Sáu mươi bốn, sáu mươi lăm. |
| key_topics[24] | SUPPORTED | no | sufficient | no | no | e5be5feaa7a9aa16617c5aa604548031 | e5be5feaa7a9aa16617c5aa604548031: Tuy nhiên, chi phí cho dự án này sẽ không rẻ chút nào. |
| open_questions[0] | SUPPORTED | no | sufficient | no | no | d3e55f0db70a772b30b5adea75335fae | d3e55f0db70a772b30b5adea75335fae: Chúa ơi, chả lẽ lão ủn ỉn cũng có trí tuệ ư? |
| confirmation_items[0] | SUPPORTED | no | sufficient | no | no | 1679017f0ba8c1d7210b25c0756c90de | 1679017f0ba8c1d7210b25c0756c90de: Khi tư vấn thì mọi người đều nói đem mẹ đến bệnh viện |

Gold fact records for this case are complete in scores.json; the table above cites the source text for every generated bullet.

## Limitation matrix

| issue | current public-source assessment | evidence and limit |
|---|---|---|
| #77 | NOT_ASSESSABLE | The public snapshots contain no meeting-level agreed decisions, owners, or deadlines. Current outputs contain zero invented owner/deadline fields, but absence of those fields is not a test of the original issue. |
| #78 | NOT_ASSESSABLE | Every current result reports one provider call and one owned span. There are no chunk boundaries, stale/final state transitions, or merge artifacts to test. |
| #79 | PUBLIC_SOURCE_SEMANTIC_EVIDENCE_ONLY | This run directly tests public source faithfulness. It found no unsupported bullet, but it has partial bullets and is not a meeting-acceptance replacement. |
| #107 | NOT_ASSESSABLE | The public snippets contain no agreed dates or group plans with owner/deadline. The only explicit source transition is represented as a confirmation item in repeated VietMed; this cannot validate the original meeting placement criterion. |
| #108 | NOT_ASSESSABLE | The inputs are 27 virtual row-position seconds or fewer, not long meetings. One-call/one-chunk outputs cannot test the >1 hour split/parallel/reduce behavior or the 69/75 criterion. |
| #110 | NOT_ASSESSABLE_FOR_ORIGINAL_MEETING | Exact duplicate text is zero in this public set, but source citation reuse and high-level overview/topic overlap are not the original meeting rubric. The public snippets lack the decisions/actions pattern needed to close this issue. |
| #88 | NOT_ASSESSABLE | The public snapshots have no speakers or meeting dates and use virtual row positions. They are not continuous meetings, real duration, a 2 hour meeting, or a 2–3 speaker sample. |

The current issue captures were read after result inspection and used only for this matrix. They do not replace source-text grading. #79 has direct public semantic evidence; the other rows remain unassessable or scope-limited.

## Evidence-source disclosure

- Source-first read and readiness record completed before opening generated results.
- No prior scoring report was opened.
- The frozen gold is not blind or human-approved. It was revised after earlier outputs and is used only as a source-derived comparison aid.
- The seven current issue captures were read after result inspection and used only for the limitation matrix.
- Preparation also included some historical repository documentation/git-history inspection before the user's correction. Those materials are excluded from per-bullet support labels; no blind-scoring claim is made. Full disclosure is in scores.json.

Machine-readable detail, including every gold fact and all source/gold tensions: scores.json.
