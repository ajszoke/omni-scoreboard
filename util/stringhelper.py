
def isStringEmpty(input):
    return input is not None and isinstance(input, str) and len(input.strip()) > 0
