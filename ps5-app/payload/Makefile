# PKG MUTANT SHOP payload — build with the ps5-payload-dev SDK.
PS5_HOST ?= 10.0.0.99
PS5_PORT ?= 9021
PMS_URL  ?= http://10.0.0.76:8710

ifdef PS5_PAYLOAD_SDK
    include $(PS5_PAYLOAD_SDK)/toolchain/prospero.mk
else
    $(error PS5_PAYLOAD_SDK is undefined — see README.md)
endif

ELF := pkg-mutant-shop.elf
CFLAGS := -Wall -g -DPMS_URL='"$(PMS_URL)"' -lSceNotification -lSceSystemService

all: $(ELF)

$(ELF): main.c
	$(CC) $(CFLAGS) -o $@ $^

clean:
	rm -f $(ELF)

test: $(ELF)
	$(PS5_DEPLOY) -h $(PS5_HOST) -p $(PS5_PORT) $^

.PHONY: all clean test
