# base-aprs-client
base layer for building more complex APRS interactions with python

## Testing with socat

```
socat TCP4-LISTEN:14588 TCP4-LISTEN:14589
```

```
python
import aprs

with aprs.TCP(
    host="localhost",
    port=14588,
    user="KF7HVM",
    passcode="-1",  # use a real passcode for TX
    command='filter r/46.1/-122.9/500',
) as aprs_tcp:
    # block until 1 frame is available and print repr
    print(repr(aprs_tcp.read(
        callback=lambda f: print(f),
        min_frames=1,
    )[0]))
```
```
python
import aprs

with aprs.TCP(
    host="localhost",
    port=14588,
    user="KF7HVM",
    passcode="-1",  # use a real passcode for TX
    command='filter r/46.1/-122.9/500',
) as aprs_tcp:
    frame = aprs.APRSFrame.from_str('KF7HVM-2>APRS:>Test from aprs!')
    aprs_tcp.write(frame)
```