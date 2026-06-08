import FamilySearch, MCR, ztzupu

source = input("Is this from Familysearch (1), MyChinaRoots (2), or ZtZupu (3)? ")

if source == "1":
    with open("FamilySearchJiapuLinks.txt", "r") as f:
        ids = [i.strip() for i in f.read().split("\n") if i != '' and i[0] != '#']
    FamilySearch.main(ids)
elif source == "2":
    with open("MCRJiapuLinks.txt", "r") as f:
        ids = [i.strip() for i in f.read().split("\n") if i != '']
    MCR.main(ids)
elif source == "3":
    with open("ZtZupuJiapuLinks.txt", "r") as f:
        ids = [i.strip() for i in f.read().split("\n") if i != '']
    ztzupu.main(ids)