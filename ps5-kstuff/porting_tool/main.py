import sys, json, threading, functools, os.path

if 'linux' not in sys.platform:
    print('This tool only supports GNU/Linux! Use Docker or WSL on other OSes.')
    input('Press Enter to exit')
    exit(1)
elif len(sys.argv) not in (3, 4, 5):
    print('usage: main.py <database> <ps5 ip> [port for payload loader] [kernel data dump]')
    exit(0)

import gdb_rpc

gdb = gdb_rpc.GDB(sys.argv[2]) if len(sys.argv) == 3 else gdb_rpc.GDB(sys.argv[2], int(sys.argv[3]))

with open(sys.argv[1]) as file:
    symbols = json.load(file)

def die(*args):
    print(*args)
    exit(1)

def set_symbol(k, v):
    assert k not in symbols or symbols[k] == v
    if k not in symbols:
        print('offset found! %s = %s'%(k, hex(v)))
        symbols[k] = v
        with open(sys.argv[1], 'w') as file:
            json.dump(symbols, file)

if 'allproc' not in symbols:
    die('`allproc` is not defined')

R0GDB_FLAGS = ['-DMEMRW_FALLBACK', '-DNO_BUILTIN_OFFSETS']

r0gdb = gdb_rpc.R0GDB(gdb, R0GDB_FLAGS)

def ostr(x):
    return str(x % 2**64)

def retry_on_error(f):
    @functools.wraps(f)
    def f1(*args):
        while True:
            try: return f(*args)
            except gdb_rpc.DisconnectedException:
                print('\nPS5 disconnected, retrying %s...'%f.__name__)
    return f1

derivations = []

def derive_symbol(f):
    derivations.append(f)
    return f

def derive_symbols(*names):
    def inner(f):
        derivations.append((f, names))
        return f
    return inner

@retry_on_error
def dump_kernel():
    if len(sys.argv) == 5 and os.path.exists(sys.argv[4]):
        with open(sys.argv[4], 'rb') as file:
            data = file.read()
        if int.from_bytes(data[8:16], 'little') == len(data) - 16:
            return data[16:], int.from_bytes(data[:8], 'little')
    gdb.use_r0gdb(R0GDB_FLAGS)
    print('dumping kdata... ', end='')
    sys.stdout.flush()
    kdata_base = gdb.ieval('kdata_base')
    gdb.eval('offsets.allproc = '+ostr(kdata_base + symbols['allproc']))
    if not gdb.ieval('rpipe'): gdb.eval('r0gdb_init_with_offsets()')
    sock, addr = gdb.bind_socket()
    with sock:
        remote_fd = gdb.ieval('r0gdb_open_socket("%s", %d)'%addr)
        remote_buf = gdb.ieval('malloc(1048576)')
        one_second = gdb.ieval('(void*)(uint64_t[2]){1, 0}')
        local_buf = bytearray()
        def appender():
            nonlocal local_buf
            with sock.accept()[0] as sock1:
                while True:
                    q = sock1.recv(4096)
                    local_buf += q
                    if not q: return
                    s = str(len(local_buf))
                    s += '\b'*len(s)
                    sys.stdout.write(s)
                    sys.stdout.flush()
        thr = threading.Thread(target=appender, daemon=True)
        thr.start()
        total_sent = 0
        while total_sent < (134 << 20):
            chk0 = gdb.ieval('copyout(%d, %d, %d)'%(remote_buf, kdata_base+total_sent, min(1048576, (134 << 20) - total_sent)))
            if chk0 <= 0: break
            offset = 0
            while offset < chk0:
                chk = gdb.ieval('(int)write(%d, %d, %d)'%(remote_fd, remote_buf+offset, chk0-offset))
                assert chk > 0
                offset += chk
                total_sent += chk
        # this loop is to detect panics while dumping
        while len(local_buf) != total_sent:
            gdb.eval('(int)nanosleep(%d)'%one_second)
        gdb.eval('(int)close(%d)'%remote_fd)
    thr.join()
    print()
    if len(sys.argv) == 5:
        with open(sys.argv[4], 'wb') as file:
            file.write(kdata_base.to_bytes(8, 'little'))
            file.write(len(local_buf).to_bytes(8, 'little'))
            file.write(local_buf)
    return bytes(local_buf), kdata_base

def get_kernel(_cache=[]):
    if not _cache:
        _cache.append(dump_kernel())
    return _cache[0]

@derive_symbol
@retry_on_error
def idt():
    kernel, kdata_base = get_kernel()
    ks = bytes(kernel[i+2:i+4] == b'\x20\x00' and kernel[i+4] < 8 and kernel[i+5] in (0x8e, 0xee) and kernel[i+8:i+16] == b'\xff\xff\xff\xff\x00\x00\x00\x00' for i in range(0, len(kernel), 16))
    offset = ks.find(b'\1'*256)
    assert ks.find(b'\1'*256, offset+1) < 0
    return offset * 16

@derive_symbol
@retry_on_error
def gdt_array():
    kernel, kdata_base = get_kernel()
    ks = kernel[5::8]
    needle = b'\x00\x00\xf3\xf3\x9b\x93\xfb\xf3\xfb\x8b\x00\x00\x00' * 16
    offset = ks.find(needle)
    assert ks.find(needle, offset+1) < 0
    return offset * 8

@derive_symbol
@retry_on_error
def tss_array():
    kernel, kdata_base = get_kernel()
    gdt_array = symbols['gdt_array']
    tss_array = []
    for i in range(16):
        j = gdt_array + 0x68 * i + 0x48
        tss_array.append(int.from_bytes(kernel[j+2:j+5]+kernel[j+7:j+12], 'little'))
    assert tss_array == list(range(tss_array[0], tss_array[-1]+0x68, 0x68))
    return tss_array[0] - kdata_base

# XXX: relies on in-structure offsets, is it ok?
@derive_symbol
@retry_on_error
def pcpu_array():
    kernel, kdata_base = get_kernel()
    planes = [b''.join(kernel[j+0x34:j+0x38]+kernel[j+0x730:j+0x738] for j in range(i, len(kernel), 0x900)) for i in range(0, 0x900, 4)]
    needle = b''.join(i.to_bytes(4, 'little')*3 for i in range(16))
    indices = [i.find(needle) for i in planes]
    unique_indices = set(indices)
    assert len(unique_indices) == 2 and -1 in unique_indices
    unique_indices.discard(-1)
    i = unique_indices.pop()
    j = indices.index(i)
    indices[j] = -1
    assert set(indices) == {-1}
    assert planes[j].find(needle, i+1) < 0
    return (i // 12) * 0x900 + j * 4

def get_string_xref(name, offset):
    kernel, kdata_base = get_kernel()
    s = kernel.find((name+'\0').encode('ascii'))
    return kernel.find((kdata_base+s).to_bytes(8, 'little')) - offset

@derive_symbol
@retry_on_error
def sysentvec(): return get_string_xref('Native SELF', 0x48)

@derive_symbol
@retry_on_error
def sysentvec_ps4(): return get_string_xref('PS4 SELF', 0x48)

def deref(name, offset=0):
    kernel, kdata_base = get_kernel()
    return int.from_bytes(kernel[symbols[name]+offset:symbols[name]+offset+8], 'little') - kdata_base

@derive_symbol
@retry_on_error
def sysents(): return deref('sysentvec', 8)

@derive_symbol
@retry_on_error
def sysents_ps4(): return deref('sysentvec_ps4', 8)

# XXX: do we need to also find (calculate?) the header size?
@derive_symbol
@retry_on_error
def mini_syscore_header():
    kernel, kdata_base = get_kernel()
    gdb.use_r0gdb(R0GDB_FLAGS)
    remote_fd = gdb.ieval('(int)open("/mini-syscore.elf", 0)')
    remote_buf = gdb.ieval('malloc(4096)')
    assert gdb.ieval('(int)read(%d, %d, 4096)'%(remote_fd, remote_buf)) == 4096
    gdb.execute('set print elements 0')
    gdb.execute('set print repeats 0')
    ans = gdb.eval('((int)close(%d), {unsigned int[1024]}%d)'%(remote_fd, remote_buf))
    assert ans.startswith('{') and ans.endswith('}') and ans.count(',') == 1023, ans
    header = b''.join(int(i).to_bytes(4, 'little') for i in ans[1:-1].split(','))
    return kernel.find(header)

# https://github.com/cheburek3000/meme_dumper/blob/main/source/main.c#L80, guess_kernel_pmap_store_offset
@derive_symbol
@retry_on_error
def kernel_pmap_store():
    kernel, kdata_base = get_kernel()
    needle = (0x1430000 | (4 << 128)).to_bytes(24, 'little')
    i = 0
    ans = []
    while True:
        i = kernel.find(needle, i)
        if i < 0: break
        if any(kernel[i+24:i+32]) and kernel[i+24:i+28] == kernel[i+32:i+36] and not any(kernel[i+36:i+40]):
            ans.append(i - 8)
        i += 1
    return ans[-1]

@derive_symbol
@retry_on_error
def crypt_singleton_array():
    kernel, kdata_base = get_kernel()
    ks = kernel[6::8]
    ks1 = kernel[7::8]
    needle = b'\xff\x00\xff\xff\xff\x00\x00\xff\x00\xff\xff\x00\x00\xff\x00\x00\x00\x00\xff\x00\xff\x00'
    offset = ks.find(needle)
    assert ks.find(needle, offset+1) < 0
    assert ks1[offset:offset+len(needle)] == needle
    return offset * 8

def virt2phys(virt, phys, addr):
    #print(hex(virt), hex(phys), hex(addr))
    assert phys == virt % 2**32
    pml = phys
    for i in range(39, 3, -9):
        idx = (addr >> i) & 511
        pml_next = gdb.ieval('{void*}%d'%(pml+idx*8+virt-phys))
        if pml_next & 128:
            ans = (pml_next & (2**48 - 2**i)) | (addr & (2**i - 1))
            break
        pml = pml_next & (2**48 - 2**12)
    else:
        ans = pml | (addr & 4095)
    #print('->', hex(ans))
    return ans

@derive_symbol
@retry_on_error
def doreti_iret():
    gdb.use_r0gdb(R0GDB_FLAGS)
    kdata_base = gdb.ieval('kdata_base')
    gdb.eval('offsets.allproc = '+ostr(kdata_base + symbols['allproc']))
    if not gdb.ieval('rpipe'): gdb.eval('r0gdb_init_with_offsets()')
    idt = kdata_base + symbols['idt']
    tss_array = kdata_base + symbols['tss_array']
    #buf = gdb.ieval('{void*}%d'%(tss_array+0x1c+4*8))
    buf = gdb.ieval('kmalloc(2048)') + 2048
    for i in range(16):
        tss = tss_array + i * 0x68
        gdb.ieval('{void*}%d = %d'%(tss+0x1c+4*8, buf))
    gdb.ieval('{char}%d = 0'%(idt+1*16+4))
    gdb.ieval('{char}%d = 4'%(idt+13*16+4))
    ptr = gdb.ieval('{void*}({void*}(get_thread()+8)+0x200)+0x300')
    virt = gdb.ieval('{void*}%d'%ptr)
    phys = gdb.ieval('{void*}%d'%(ptr+8))
    buf_phys = virt2phys(virt, phys, buf)
    pages = set()
    while True:
        page = gdb.ieval('kmalloc(2048)') & -4096
        if page in pages: break
        pages.add(page)
    gdb.ieval('(void*)({void*[512]}%d = {%s})'%(page, ', '.join(map(str, ((i<<39)|135 for i in range(512))))))
    gdb.ieval('{void*}%d = %d'%(virt+8, virt2phys(virt, phys, page)|7))
    buf_alias = buf_phys | (1 << 39)
    #print(hex(buf), hex(buf_alias))
    gdb.eval('bind_to_all_available_cpus()')
    assert not gdb.ieval('(int)pthread_create(malloc(8), 0, hammer_thread, (uint64_t[2]){%d, malloc(65536)+65536})'%(buf_alias-32))
    assert not gdb.ieval('bind_to_some_cpu(0)')
    if 'Remote connection closed' in gdb.eval('jmp_setcontext(1ull<<50)'):
        raise gdb_rpc.DisconnectedException('jmp_setcontext')
    pc = gdb.ieval('$pc')
    gdb.kill()
    assert (pc >> 32) == 16
    pc |= (2**64 - 2**32)
    return pc - kdata_base

def do_use_r0gdb_raw():
    kdata_base = gdb.ieval('kdata_base')
    gdb.eval('offsets.allproc = '+ostr(kdata_base + symbols['allproc']))
    if not gdb.ieval('rpipe'): gdb.eval('r0gdb_init_with_offsets()')
    gdb.eval('offsets.doreti_iret = '+ostr(kdata_base + symbols['doreti_iret']))
    gdb.eval('offsets.add_rsp_iret = offsets.doreti_iret - 7')
    gdb.eval('offsets.swapgs_add_rsp_iret = offsets.add_rsp_iret - 3')
    gdb.eval('offsets.idt = '+ostr(kdata_base + symbols['idt']))
    gdb.eval('offsets.tss_array = '+ostr(kdata_base + symbols['tss_array']))

use_r0gdb_raw = r0gdb.use_raw_fn(do_use_r0gdb_raw)

@derive_symbols('push_pop_all_iret', 'rdmsr_start', 'pop_all_iret', 'justreturn')
@retry_on_error
def justreturn():
    use_r0gdb_raw()
    kdata_base = gdb.ieval('kdata_base')
    idt = kdata_base + symbols['idt']
    int244 = (gdb.ieval('{void*}%d'%(idt+244*16+6), 5) % 2**48) * 2**16 + gdb.ieval('{unsigned short}%d'%(idt+244*16), 5)
    print('single-stepping...')
    def step():
        gdb.execute('stepi', 15)
        print(hex(gdb.ieval('$pc')), hex(gdb.ieval('$rsp')))
    gdb.ieval('$pc = %d'%int244)
    step()
    step()
    # step until rdmsr
    rsp0 = gdb.ieval('$rsp')
    rax = gdb.ieval('$rax')
    rdx = gdb.ieval('$rdx')
    pc = gdb.ieval('$pc')
    while True:
        step()
        assert gdb.ieval('$rsp') == rsp0
        if gdb.ieval('$rax') != rax and gdb.ieval('$rdx') != rdx:
            break
        pc = gdb.ieval('$pc')
    rdmsr = pc
    assert gdb.ieval('$pc') == rdmsr + 2
    # step until the function call & through it
    while gdb.ieval('$rsp') == rsp0: step()
    while gdb.ieval('$rsp') != rsp0: step()
    pc = gdb.ieval('$pc')
    step()
    # check that we actually jumped (somewhere...)
    assert (gdb.ieval('$pc') - pc) % 2**64 >= 16
    justreturn = gdb.ieval('$pc') - 16
    gdb.ieval('{void*}$rsp = 0x1337133713371337')
    # step until ld_regs
    while gdb.ieval('$rdi') != 0x1337133713371337:
        pc = gdb.ieval('$pc')
        step()
    pop_all_iret = pc
    # sanity check on justreturn
    rsp0 = gdb.ieval('$rsp')
    gdb.ieval('$pc = %d'%justreturn)
    gdb.ieval('$rax = 0x4141414142424242')
    step()
    assert gdb.ieval('$rsp') == rsp0 - 8 and gdb.ieval('{void*}$rsp') == 0x4141414142424242
    return int244-kdata_base, rdmsr-kdata_base, pop_all_iret-kdata_base, justreturn-kdata_base

@derive_symbol
@retry_on_error
def wrmsr_ret():
    use_r0gdb_raw()
    kdata_base = gdb.ieval('kdata_base')
    gdb.ieval('$pc = %d'%(kdata_base+symbols['justreturn']))
    print('single-stepping...')
    while gdb.ieval('($eflags = 0x102, $rcx)') != 0x80b:
        gdb.execute('stepi')
        print(hex(gdb.ieval('$pc')), hex(gdb.ieval('$rsp')))
    gdb.execute('stepi')
    gdb.execute('stepi')
    wrmsr = gdb.ieval('$pc')
    try: gdb.execute('stepi')
    except gdb_rpc.DisconnectedException: pass
    else: assert False
    return wrmsr-kdata_base

def do_use_r0gdb_trace():
    do_use_r0gdb_raw()
    kdata_base = gdb.ieval('kdata_base')
    gdb.ieval('offsets.rdmsr_start = %d'%(kdata_base+symbols['rdmsr_start']))
    gdb.ieval('offsets.wrmsr_ret = %d'%(kdata_base+symbols['wrmsr_ret']))
    if 'rep_movsb_pop_rbp_ret' in symbols:
        gdb.ieval('offsets.rep_movsb_pop_rbp_ret = %d'%(kdata_base+symbols['rep_movsb_pop_rbp_ret']))

use_r0gdb_trace = r0gdb.use_trace_fn(do_use_r0gdb_trace)

@derive_symbol
@retry_on_error
def rep_movsb_pop_rbp_ret():
    use_r0gdb_trace(0)
    kdata_base = gdb.ieval('kdata_base')
    pc0 = gdb.ieval('$pc = (void*)dlsym(0x2001, "getpid")')
    ptr = gdb.ieval('ptr_to_leaked_rep_movsq = kmalloc(8)')
    gdb.ieval('trace_prog = leak_rep_movsq')
    gdb.execute('stepi')
    assert gdb.ieval('$pc') == pc0 + 12
    rep_movsq = gdb.ieval('{void*}%d'%ptr)
    r0gdb.trace_to_raw()
    # trace from rep movsq to nearby rep movsb
    rdi = rsi = gdb.ieval('($pc = %d, $rdi = $rsi = $rsp)'%rep_movsq) % 2**64
    while True:
        pc = gdb.ieval('($rcx = 1, $pc)')
        print(hex(pc), hex(rdi), hex(rsi))
        gdb.execute('stepi')
        rdi1 = gdb.ieval('$rdi') % 2**64
        rsi1 = gdb.ieval('$rsi') % 2**64
        if rdi1 == rdi + 1 and rsi1 == rsi + 1 and gdb.ieval('$rcx') == 0:
            break
        rdi = rdi1
        rsi = rsi1
    rep_movsb = pc
    # check epilogue
    gdb.ieval('{void*}$rsp = 0x1234')
    gdb.ieval('{void*}($rsp+8) = 0x5678')
    gdb.execute('stepi')
    gdb.execute('stepi')
    assert gdb.ieval('$rbp == 0x1234 && $rip == 0x5678')
    return rep_movsb - kdata_base

print(len(symbols), 'offsets currently known')
print(sum(sum(j not in symbols for j in i[1]) if isinstance(i, tuple) else (i.__name__ not in symbols) for i in derivations), 'offsets to be found')

for i in derivations:
    if isinstance(i, tuple):
        i, names = i
        if any(j not in symbols for j in names):
            print('Probing offsets `%s`'%'`, `'.join(names))
            try: value = i()
            except Exception:
                raise Exception("failed to derive `%s`, see above why"%'`, `'.join(names))
            assert len(value) == len(names)
            for i, j in zip(names, value):
                set_symbol(i, j)
    elif i.__name__ not in symbols:
        print('Probing offset `%s`'%i.__name__)
        try: value = i()
        except Exception:
            raise Exception("failed to derive `%s`, see above why"%i.__name__)
        set_symbol(i.__name__, value)
